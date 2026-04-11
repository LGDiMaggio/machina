"""iCal backend — parse .ics files and URLs (read-only).

Supports local ``.ics`` files and HTTP(S) URLs.  Recurring events are
expanded using ``python-dateutil`` ``rrule``.  This backend is read-only;
``create_event`` and ``delete_event`` raise :class:`ConnectorError`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from machina.domain.calendar import CalendarEvent, EventType
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


class ICalBackend:
    """Read-only backend that parses iCal (.ics) data.

    Args:
        source: Local file path or HTTP(S) URL to an ``.ics`` file.
        calendar_type_map: Mapping of calendar summary keywords to
            :class:`EventType` values.
    """

    def __init__(
        self,
        *,
        source: str = "",
        calendar_type_map: dict[str, EventType] | None = None,
    ) -> None:
        self._source = source
        self._calendar_type_map = calendar_type_map or {}
        self._raw_data: str | None = None

    async def connect(self) -> None:
        """Load and cache the iCal data from file or URL."""
        if not self._source:
            raise ConnectorError("source is required for iCal backend")

        try:
            import icalendar  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            msg = (
                "icalendar is required for the iCal backend. "
                "Install with: pip install machina-ai[ical]"
            )
            raise ImportError(msg) from None

        self._raw_data = await asyncio.to_thread(self._load_source)
        logger.info(
            "connected", connector="CalendarConnector", backend="ical", source=self._source
        )

    def _load_source(self) -> str:
        """Synchronously load iCal data from file or URL."""
        if self._source.startswith(("http://", "https://")):
            import urllib.request

            with urllib.request.urlopen(self._source, timeout=30) as resp:
                return resp.read().decode("utf-8")

        path = Path(self._source)
        if not path.exists():
            raise ConnectorError(f"iCal file not found: {self._source}")
        return path.read_text(encoding="utf-8")

    async def disconnect(self) -> None:
        """Clear cached data."""
        self._raw_data = None
        logger.info("disconnected", connector="CalendarConnector", backend="ical")

    async def list_events(
        self,
        calendar_id: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CalendarEvent]:
        """Parse iCal data and return events within the time range.

        Args:
            calendar_id: Ignored for iCal (single calendar per file).
            start: Filter events starting at or after this time.
            end: Filter events ending at or before this time.

        Returns:
            List of :class:`CalendarEvent` instances.
        """
        if self._raw_data is None:
            raise ConnectorError("Not connected — call connect() first")

        return await asyncio.to_thread(self._parse_events, start, end)

    def _parse_events(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> list[CalendarEvent]:
        """Synchronously parse the cached iCal data."""
        import icalendar  # type: ignore[import-not-found]

        cal = icalendar.Calendar.from_ical(self._raw_data)
        events: list[CalendarEvent] = []

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            parsed = self._vevent_to_events(component, start, end)
            events.extend(parsed)

        logger.debug(
            "events_parsed",
            connector="CalendarConnector",
            backend="ical",
            count=len(events),
        )
        return events

    def _vevent_to_events(
        self,
        component: Any,
        start: datetime | None,
        end: datetime | None,
    ) -> list[CalendarEvent]:
        """Convert a VEVENT (possibly recurring) into CalendarEvent(s)."""

        uid = str(component.get("uid", ""))
        summary = str(component.get("summary", ""))
        description = str(component.get("description", ""))
        location = str(component.get("location", ""))

        dtstart = component.get("dtstart")
        dtend = component.get("dtend")
        if dtstart is None:
            return []

        dtstart_val = dtstart.dt
        dtend_val = dtend.dt if dtend else dtstart_val

        # Determine if all-day (date vs datetime)
        all_day = not isinstance(dtstart_val, datetime)
        if all_day:
            dtstart_val = datetime.combine(dtstart_val, datetime.min.time(), tzinfo=UTC)
            dtend_val = datetime.combine(dtend_val, datetime.min.time(), tzinfo=UTC)
        else:
            if dtstart_val.tzinfo is None:
                dtstart_val = dtstart_val.replace(tzinfo=UTC)
            if dtend_val.tzinfo is None:
                dtend_val = dtend_val.replace(tzinfo=UTC)

        duration = dtend_val - dtstart_val
        event_type = self._classify_event(summary)
        rrule_str = ""

        rrule = component.get("rrule")
        if rrule and start and end:
            rrule_str = rrule.to_ical().decode("utf-8")
            return self._expand_rrule(
                uid=uid,
                summary=summary,
                description=description,
                location=location,
                dtstart=dtstart_val,
                duration=duration,
                rrule_text=rrule_str,
                all_day=all_day,
                event_type=event_type,
                range_start=start,
                range_end=end,
            )

        # Non-recurring: apply date filter
        if start and dtend_val < start:
            return []
        if end and dtstart_val > end:
            return []

        return [
            CalendarEvent(
                id=uid,
                title=summary,
                start=dtstart_val,
                end=dtend_val,
                event_type=event_type,
                description=description,
                location=location,
                all_day=all_day,
                recurring=False,
                recurrence_rule=rrule_str,
            )
        ]

    def _expand_rrule(
        self,
        *,
        uid: str,
        summary: str,
        description: str,
        location: str,
        dtstart: datetime,
        duration: Any,
        rrule_text: str,
        all_day: bool,
        event_type: EventType,
        range_start: datetime,
        range_end: datetime,
        max_occurrences: int = 500,
    ) -> list[CalendarEvent]:
        """Expand a recurring event using python-dateutil rrule."""
        from dateutil.rrule import rrulestr  # type: ignore[import-not-found]

        rule = rrulestr(f"RRULE:{rrule_text}", dtstart=dtstart)
        occurrences = rule.between(range_start, range_end, inc=True)

        events: list[CalendarEvent] = []
        for i, occ in enumerate(occurrences):
            if i >= max_occurrences:
                break
            occ_start = occ if occ.tzinfo else occ.replace(tzinfo=UTC)
            occ_end = occ_start + duration
            events.append(
                CalendarEvent(
                    id=f"{uid}_{i}",
                    title=summary,
                    start=occ_start,
                    end=occ_end,
                    event_type=event_type,
                    description=description,
                    location=location,
                    all_day=all_day,
                    recurring=True,
                    recurrence_rule=rrule_text,
                )
            )
        return events

    def _classify_event(self, summary: str) -> EventType:
        """Classify an event based on summary keywords and calendar_type_map."""
        lower = summary.lower()
        for keyword, etype in self._calendar_type_map.items():
            if keyword.lower() in lower:
                return etype
        return EventType.OTHER

    async def create_event(self, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        """Not supported — iCal backend is read-only."""
        raise ConnectorError("iCal backend is read-only — cannot create events")

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Not supported — iCal backend is read-only."""
        raise ConnectorError("iCal backend is read-only — cannot delete events")

    @property
    def is_connected(self) -> bool:
        """Whether iCal data has been loaded."""
        return self._raw_data is not None
