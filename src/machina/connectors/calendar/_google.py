"""Google Calendar backend — OAuth2 + Google Calendar API v3.

Supports both service-account and installed-app authentication flows.
All blocking Google SDK calls are wrapped in ``asyncio.to_thread()``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from machina.domain.calendar import CalendarEvent, EventType
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)


class GoogleCalendarBackend:
    """Backend for Google Calendar API v3.

    Args:
        credentials_file: Path to OAuth2 client-secrets JSON
            (installed-app flow).
        service_account_file: Path to service-account key JSON
            (server-to-server flow).  Takes precedence over
            ``credentials_file``.
        calendar_type_map: Mapping of calendar IDs to
            :class:`EventType` values.
        scopes: OAuth2 scopes (defaults to read/write).
    """

    _DEFAULT_SCOPES: ClassVar[list[str]] = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ]

    def __init__(
        self,
        *,
        credentials_file: str = "",
        service_account_file: str = "",
        calendar_type_map: dict[str, EventType] | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        self._credentials_file = credentials_file
        self._service_account_file = service_account_file
        self._calendar_type_map = calendar_type_map or {}
        self._scopes = scopes or self._DEFAULT_SCOPES
        self._service: Any = None

    async def connect(self) -> None:
        """Authenticate and create the Google Calendar service."""
        try:
            from googleapiclient.discovery import build
        except ImportError:
            msg = (
                "Google API libraries are required for Google Calendar backend. "
                "Install with: pip install machina-ai[google-calendar]"
            )
            raise ImportError(msg) from None

        if self._service_account_file:
            creds = await asyncio.to_thread(self._auth_service_account)
        elif self._credentials_file:
            creds = await asyncio.to_thread(self._auth_installed_app)
        else:
            raise ConnectorAuthError(
                "Either service_account_file or credentials_file is required "
                "for Google Calendar backend"
            )

        self._service = await asyncio.to_thread(build, "calendar", "v3", credentials=creds)
        logger.info("connected", connector="CalendarConnector", backend="google")

    def _auth_service_account(self) -> Any:
        """Authenticate via service-account key file."""
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            self._service_account_file, scopes=self._scopes
        )

    def _auth_installed_app(self) -> Any:
        """Authenticate via installed-app OAuth2 flow."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            self._credentials_file, scopes=self._scopes
        )
        return flow.run_local_server(port=0)

    async def disconnect(self) -> None:
        """Release the Google Calendar service."""
        self._service = None
        logger.info("disconnected", connector="CalendarConnector", backend="google")

    async def list_events(
        self,
        calendar_id: str = "primary",
        start: datetime | None = None,
        end: datetime | None = None,
        max_results: int = 250,
    ) -> list[CalendarEvent]:
        """Fetch events from a Google Calendar.

        Args:
            calendar_id: Google Calendar ID (``"primary"`` for the
                authenticated user's main calendar).
            start: Lower bound filter (inclusive).
            end: Upper bound filter (inclusive).
            max_results: Maximum events to return.

        Returns:
            List of :class:`CalendarEvent` instances.
        """
        if self._service is None:
            raise ConnectorError("Not connected — call connect() first")

        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if start:
            kwargs["timeMin"] = start.isoformat()
        if end:
            kwargs["timeMax"] = end.isoformat()

        result = await asyncio.to_thread(self._service.events().list(**kwargs).execute)

        event_type = self._calendar_type_map.get(calendar_id, EventType.OTHER)
        events: list[CalendarEvent] = []
        for item in result.get("items", []):
            events.append(self._to_calendar_event(item, calendar_id, event_type))

        logger.debug(
            "events_listed",
            connector="CalendarConnector",
            backend="google",
            calendar_id=calendar_id,
            count=len(events),
        )
        return events

    async def create_event(
        self,
        calendar_id: str,
        event: CalendarEvent,
    ) -> CalendarEvent:
        """Create an event in a Google Calendar.

        Args:
            calendar_id: Target Google Calendar ID.
            event: The event to create.

        Returns:
            The created event with its Google-assigned ID.
        """
        if self._service is None:
            raise ConnectorError("Not connected — call connect() first")

        body = self._to_google_body(event)
        result = await asyncio.to_thread(
            self._service.events().insert(calendarId=calendar_id, body=body).execute
        )

        event_type = self._calendar_type_map.get(calendar_id, event.event_type)
        created = self._to_calendar_event(result, calendar_id, event_type)
        logger.info(
            "event_created",
            connector="CalendarConnector",
            backend="google",
            event_id=created.id,
        )
        return created

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete an event from a Google Calendar.

        Args:
            calendar_id: Google Calendar ID.
            event_id: Event ID to delete.
        """
        if self._service is None:
            raise ConnectorError("Not connected — call connect() first")

        await asyncio.to_thread(
            self._service.events().delete(calendarId=calendar_id, eventId=event_id).execute
        )
        logger.info(
            "event_deleted",
            connector="CalendarConnector",
            backend="google",
            event_id=event_id,
        )

    @property
    def is_connected(self) -> bool:
        """Whether the Google service is initialised."""
        return self._service is not None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_calendar_event(
        item: dict[str, Any],
        calendar_id: str,
        event_type: EventType,
    ) -> CalendarEvent:
        """Convert a Google Calendar API event dict to a CalendarEvent."""
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})

        # Google uses "dateTime" for timed events and "date" for all-day
        all_day = "date" in start_raw and "dateTime" not in start_raw

        if all_day:
            start_dt = datetime.fromisoformat(start_raw["date"])
            start_dt = datetime.combine(start_dt, datetime.min.time(), tzinfo=UTC)
            end_dt = datetime.fromisoformat(end_raw.get("date", start_raw["date"]))
            end_dt = datetime.combine(end_dt, datetime.min.time(), tzinfo=UTC)
        else:
            start_dt = datetime.fromisoformat(start_raw.get("dateTime", ""))
            end_dt = datetime.fromisoformat(end_raw.get("dateTime", ""))

        attendees = [a.get("email", "") for a in item.get("attendees", [])]
        recurrence = item.get("recurrence", [])

        return CalendarEvent(
            id=item.get("id", ""),
            title=item.get("summary", ""),
            start=start_dt,
            end=end_dt,
            event_type=event_type,
            calendar_id=calendar_id,
            description=item.get("description", ""),
            location=item.get("location", ""),
            all_day=all_day,
            recurring=bool(item.get("recurringEventId")),
            recurrence_rule=recurrence[0] if recurrence else "",
            attendees=attendees,
            metadata={"google_id": item.get("id", ""), "html_link": item.get("htmlLink", "")},
        )

    @staticmethod
    def _to_google_body(event: CalendarEvent) -> dict[str, Any]:
        """Convert a CalendarEvent to a Google Calendar API request body."""
        body: dict[str, Any] = {
            "summary": event.title,
            "description": event.description,
            "location": event.location,
        }

        if event.all_day:
            body["start"] = {"date": event.start.strftime("%Y-%m-%d")}
            body["end"] = {"date": event.end.strftime("%Y-%m-%d")}
        else:
            body["start"] = {"dateTime": event.start.isoformat()}
            body["end"] = {"dateTime": event.end.isoformat()}

        if event.attendees:
            body["attendees"] = [{"email": a} for a in event.attendees]

        if event.recurrence_rule:
            body["recurrence"] = [event.recurrence_rule]

        return body
