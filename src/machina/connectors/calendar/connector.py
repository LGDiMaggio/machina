"""CalendarConnector — unified access to Google Calendar, Outlook, and iCal.

Provides production schedules, shift patterns, technician availability,
and planned downtime windows to the Machina agent layer and
:class:`~machina.domain.services.maintenance_scheduler.MaintenanceScheduler`.

Supports three pluggable backends selected via the ``backend`` parameter:

* ``"google"`` — Google Calendar API v3 (OAuth2, service account)
* ``"outlook"`` — Microsoft 365 / Outlook (MSAL + Graph API)
* ``"ical"`` — iCal ``.ics`` files or URLs (read-only)

Install the backend you need::

    pip install machina-ai[google-calendar]    # Google
    pip install machina-ai[outlook-calendar]   # Outlook
    pip install machina-ai[ical]               # iCal
    pip install machina-ai[calendar]           # All three
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.domain.calendar import CalendarEvent, EventType
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)

_BACKENDS = frozenset({"google", "outlook", "ical"})

_FULL_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.READ_CALENDAR_EVENTS,
        Capability.CREATE_CALENDAR_EVENT,
        Capability.DELETE_CALENDAR_EVENT,
    }
)
_READONLY_CAPABILITIES: frozenset[Capability] = frozenset({Capability.READ_CALENDAR_EVENTS})


class CalendarConnector:
    """Connector for calendar and scheduling data.

    Reads (and optionally creates/deletes) calendar events from Google
    Calendar, Microsoft Outlook, or iCal sources.  Events are returned
    as :class:`~machina.domain.calendar.CalendarEvent` domain entities.

    Convenience methods filter events by :class:`EventType` so the agent
    can quickly retrieve production schedules, planned downtime, and
    technician availability.

    Args:
        backend: Calendar backend — ``"google"``, ``"outlook"``, or
            ``"ical"``.  Defaults to ``"ical"``.
        calendar_type_map: Mapping of calendar IDs (or summary keywords
            for iCal) to :class:`EventType` values for automatic
            classification.
        **kwargs: Backend-specific parameters forwarded to the selected
            backend class.

    Example:
        ```python
        from machina.connectors.calendar import CalendarConnector

        # iCal — read a local .ics file
        cal = CalendarConnector(
            backend="ical",
            source="/data/production_schedule.ics",
            calendar_type_map={"shutdown": EventType.DOWNTIME},
        )
        await cal.connect()
        events = await cal.read_events(start=..., end=...)

        # Google Calendar
        cal = CalendarConnector(
            backend="google",
            service_account_file="/secrets/sa-key.json",
            calendar_type_map={"prod-cal-id": EventType.PRODUCTION},
        )
        ```
    """

    def __init__(
        self,
        *,
        backend: str = "ical",
        calendar_type_map: dict[str, EventType] | None = None,
        **kwargs: Any,
    ) -> None:
        if backend not in _BACKENDS:
            raise ConnectorError(
                f"Unknown calendar backend {backend!r}. "
                f"Choose from: {', '.join(sorted(_BACKENDS))}"
            )

        self._backend_name = backend
        self._calendar_type_map = calendar_type_map or {}
        self._backend: Any = None

        # Set capabilities based on backend
        if backend == "ical":
            self.capabilities: frozenset[Capability] = _READONLY_CAPABILITIES
        else:
            self.capabilities = _FULL_CAPABILITIES

        # Defer backend construction to connect() to keep __init__ cheap
        self._backend_kwargs = {
            "calendar_type_map": self._calendar_type_map,
            **kwargs,
        }

    async def connect(self) -> None:
        """Initialise the backend and establish a connection."""
        self._backend = self._create_backend()
        await self._backend.connect()
        logger.info(
            "connected",
            connector="CalendarConnector",
            backend=self._backend_name,
        )

    def _create_backend(self) -> Any:
        """Instantiate the appropriate backend class."""
        if self._backend_name == "google":
            from machina.connectors.calendar._google import GoogleCalendarBackend

            return GoogleCalendarBackend(**self._backend_kwargs)
        if self._backend_name == "outlook":
            from machina.connectors.calendar._outlook import OutlookCalendarBackend

            return OutlookCalendarBackend(**self._backend_kwargs)
        # ical
        from machina.connectors.calendar._ical import ICalBackend

        return ICalBackend(**self._backend_kwargs)

    async def disconnect(self) -> None:
        """Gracefully close the backend connection."""
        if self._backend is not None:
            await self._backend.disconnect()
            self._backend = None
        logger.info(
            "disconnected",
            connector="CalendarConnector",
            backend=self._backend_name,
        )

    async def health_check(self) -> ConnectorHealth:
        """Check whether the backend is connected and operational."""
        if self._backend is None or not self._backend.is_connected:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="Not connected",
            )
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message=f"Connected ({self._backend_name} backend)",
        )

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    async def read_events(
        self,
        *,
        calendar_id: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
        event_type: EventType | None = None,
        max_results: int = 250,
    ) -> list[CalendarEvent]:
        """Read calendar events, optionally filtered by type and time range.

        Args:
            calendar_id: Calendar to read from (backend-specific).
            start: Only include events starting at or after this time.
            end: Only include events ending at or before this time.
            event_type: Filter to a specific :class:`EventType`.
            max_results: Maximum events to return.

        Returns:
            List of :class:`CalendarEvent` instances.
        """
        self._ensure_connected()

        if self._backend_name == "ical":
            events = await self._backend.list_events(calendar_id=calendar_id, start=start, end=end)
        else:
            events = await self._backend.list_events(
                calendar_id=calendar_id, start=start, end=end, max_results=max_results
            )

        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]

        return list(events)

    async def create_event(self, event: CalendarEvent, calendar_id: str = "") -> CalendarEvent:
        """Create a calendar event.

        Args:
            event: The event to create.
            calendar_id: Target calendar (backend-specific).

        Returns:
            The created event (with backend-assigned ID).

        Raises:
            ConnectorError: If the backend is read-only (iCal).
        """
        self._ensure_connected()
        result: CalendarEvent = await self._backend.create_event(calendar_id, event)
        return result

    async def delete_event(self, event_id: str, calendar_id: str = "") -> None:
        """Delete a calendar event.

        Args:
            event_id: Event identifier.
            calendar_id: Calendar containing the event (backend-specific).

        Raises:
            ConnectorError: If the backend is read-only (iCal).
        """
        self._ensure_connected()
        await self._backend.delete_event(calendar_id, event_id)

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    async def get_production_schedule(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        calendar_id: str = "",
    ) -> list[CalendarEvent]:
        """Return production-schedule events within the time range.

        Args:
            start: Lower bound filter.
            end: Upper bound filter.
            calendar_id: Calendar ID to read from.

        Returns:
            Events classified as :attr:`EventType.PRODUCTION`.
        """
        return await self.read_events(
            calendar_id=calendar_id,
            start=start,
            end=end,
            event_type=EventType.PRODUCTION,
        )

    async def get_planned_downtime(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        calendar_id: str = "",
    ) -> list[CalendarEvent]:
        """Return planned-downtime events within the time range.

        Args:
            start: Lower bound filter.
            end: Upper bound filter.
            calendar_id: Calendar ID to read from.

        Returns:
            Events classified as :attr:`EventType.DOWNTIME`.
        """
        return await self.read_events(
            calendar_id=calendar_id,
            start=start,
            end=end,
            event_type=EventType.DOWNTIME,
        )

    async def get_technician_availability(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        calendar_id: str = "",
    ) -> list[CalendarEvent]:
        """Return shift / technician-availability events.

        Args:
            start: Lower bound filter.
            end: Upper bound filter.
            calendar_id: Calendar ID to read from.

        Returns:
            Events classified as :attr:`EventType.SHIFT`.
        """
        return await self.read_events(
            calendar_id=calendar_id,
            start=start,
            end=end,
            event_type=EventType.SHIFT,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._backend is None:
            raise ConnectorError("Not connected — call connect() first")
