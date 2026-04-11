"""Outlook / Microsoft 365 backend — MSAL + Microsoft Graph API.

Authenticates via MSAL ``ConfidentialClientApplication`` (client-credentials
grant) and calls the Microsoft Graph ``/calendars`` endpoints with
``httpx.AsyncClient``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from machina.domain.calendar import CalendarEvent, EventType
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class OutlookCalendarBackend:
    """Backend for Microsoft 365 / Outlook via Graph API.

    Args:
        tenant_id: Azure AD tenant ID.
        client_id: Application (client) ID registered in Azure AD.
        client_secret: Client secret for the registered application.
        user_id: The user mailbox to access (e.g. ``"user@company.com"``
            or ``"me"`` for delegated auth).  Defaults to ``"me"``.
        calendar_type_map: Mapping of calendar IDs to
            :class:`EventType` values.
    """

    _SCOPES: ClassVar[list[str]] = ["https://graph.microsoft.com/.default"]

    def __init__(
        self,
        *,
        tenant_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        user_id: str = "me",
        calendar_type_map: dict[str, EventType] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_id = user_id
        self._calendar_type_map = calendar_type_map or {}
        self._token: str = ""
        self._http: Any = None  # httpx.AsyncClient

    async def connect(self) -> None:
        """Acquire an access token via MSAL and create an HTTP client."""
        try:
            import msal  # type: ignore[import-not-found]
        except ImportError:
            msg = (
                "msal is required for the Outlook calendar backend. "
                "Install with: pip install machina-ai[outlook-calendar]"
            )
            raise ImportError(msg) from None

        if not all([self._tenant_id, self._client_id, self._client_secret]):
            raise ConnectorAuthError(
                "tenant_id, client_id, and client_secret are required for Outlook calendar backend"
            )

        app = msal.ConfidentialClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
            client_credential=self._client_secret,
        )
        result = app.acquire_token_for_client(scopes=self._SCOPES)
        if "access_token" not in result:
            error_desc = result.get("error_description", "Unknown MSAL error")
            raise ConnectorAuthError(f"Failed to acquire Outlook token: {error_desc}")

        self._token = result["access_token"]

        import httpx

        self._http = httpx.AsyncClient(
            base_url=_GRAPH_BASE,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        )
        logger.info("connected", connector="CalendarConnector", backend="outlook")

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._token = ""
        logger.info("disconnected", connector="CalendarConnector", backend="outlook")

    async def list_events(
        self,
        calendar_id: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
        max_results: int = 250,
    ) -> list[CalendarEvent]:
        """Fetch events from an Outlook calendar via Graph API.

        Args:
            calendar_id: Outlook calendar ID.  If empty, uses the
                user's default calendar.
            start: Lower bound filter (inclusive).
            end: Upper bound filter (inclusive).
            max_results: Maximum events to return.

        Returns:
            List of :class:`CalendarEvent` instances.
        """
        if self._http is None:
            raise ConnectorError("Not connected — call connect() first")

        if calendar_id:
            url = f"/users/{self._user_id}/calendars/{calendar_id}/events"
        else:
            url = f"/users/{self._user_id}/calendar/events"

        params: dict[str, Any] = {"$top": max_results, "$orderby": "start/dateTime"}
        if start:
            params["$filter"] = f"start/dateTime ge '{start.isoformat()}'"
            if end:
                params["$filter"] += f" and end/dateTime le '{end.isoformat()}'"
        elif end:
            params["$filter"] = f"end/dateTime le '{end.isoformat()}'"

        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        event_type = self._calendar_type_map.get(calendar_id, EventType.OTHER)
        events = [
            self._to_calendar_event(item, calendar_id, event_type)
            for item in data.get("value", [])
        ]

        logger.debug(
            "events_listed",
            connector="CalendarConnector",
            backend="outlook",
            calendar_id=calendar_id,
            count=len(events),
        )
        return events

    async def create_event(
        self,
        calendar_id: str,
        event: CalendarEvent,
    ) -> CalendarEvent:
        """Create an event in an Outlook calendar.

        Args:
            calendar_id: Target Outlook calendar ID.
            event: The event to create.

        Returns:
            The created event with its Graph-assigned ID.
        """
        if self._http is None:
            raise ConnectorError("Not connected — call connect() first")

        if calendar_id:
            url = f"/users/{self._user_id}/calendars/{calendar_id}/events"
        else:
            url = f"/users/{self._user_id}/calendar/events"

        body = self._to_graph_body(event)
        resp = await self._http.post(url, json=body)
        resp.raise_for_status()
        result = resp.json()

        event_type = self._calendar_type_map.get(calendar_id, event.event_type)
        created = self._to_calendar_event(result, calendar_id, event_type)
        logger.info(
            "event_created",
            connector="CalendarConnector",
            backend="outlook",
            event_id=created.id,
        )
        return created

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete an event from an Outlook calendar.

        Args:
            calendar_id: Outlook calendar ID (unused — events are
                deleted by their global ID).
            event_id: Graph event ID to delete.
        """
        if self._http is None:
            raise ConnectorError("Not connected — call connect() first")

        resp = await self._http.delete(f"/users/{self._user_id}/events/{event_id}")
        resp.raise_for_status()
        logger.info(
            "event_deleted",
            connector="CalendarConnector",
            backend="outlook",
            event_id=event_id,
        )

    @property
    def is_connected(self) -> bool:
        """Whether the HTTP client is initialised."""
        return self._http is not None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_calendar_event(
        item: dict[str, Any],
        calendar_id: str,
        event_type: EventType,
    ) -> CalendarEvent:
        """Convert a Graph API event dict to a CalendarEvent."""
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})

        all_day = item.get("isAllDay", False)

        start_dt = datetime.fromisoformat(start_raw.get("dateTime", ""))
        end_dt = datetime.fromisoformat(end_raw.get("dateTime", ""))

        # Graph API returns naive datetimes with a timeZone field
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)

        attendees = [
            a.get("emailAddress", {}).get("address", "") for a in item.get("attendees", [])
        ]
        recurrence = item.get("recurrence")

        return CalendarEvent(
            id=item.get("id", ""),
            title=item.get("subject", ""),
            start=start_dt,
            end=end_dt,
            event_type=event_type,
            calendar_id=calendar_id,
            description=item.get("bodyPreview", ""),
            location=item.get("location", {}).get("displayName", ""),
            all_day=all_day,
            recurring=bool(item.get("seriesMasterId")),
            recurrence_rule=str(recurrence) if recurrence else "",
            attendees=attendees,
            metadata={"graph_id": item.get("id", ""), "web_link": item.get("webLink", "")},
        )

    @staticmethod
    def _to_graph_body(event: CalendarEvent) -> dict[str, Any]:
        """Convert a CalendarEvent to a Graph API request body."""
        body: dict[str, Any] = {
            "subject": event.title,
            "body": {"contentType": "text", "content": event.description},
            "start": {"dateTime": event.start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": event.end.isoformat(), "timeZone": "UTC"},
            "isAllDay": event.all_day,
        }

        if event.location:
            body["location"] = {"displayName": event.location}

        if event.attendees:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in event.attendees
            ]

        return body
