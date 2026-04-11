"""Tests for CalendarConnector and its backends."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.calendar.connector import CalendarConnector
from machina.domain.calendar import CalendarEvent, EventType
from machina.exceptions import ConnectorAuthError, ConnectorError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    id: str = "evt-1",
    title: str = "Test",
    event_type: EventType = EventType.OTHER,
    **kwargs: Any,
) -> CalendarEvent:
    return CalendarEvent(
        id=id,
        title=title,
        start=kwargs.get("start", datetime(2026, 4, 15, 6, 0, tzinfo=UTC)),
        end=kwargs.get("end", datetime(2026, 4, 15, 14, 0, tzinfo=UTC)),
        event_type=event_type,
        **{k: v for k, v in kwargs.items() if k not in ("start", "end")},
    )


SAMPLE_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:evt-ics-001
SUMMARY:Morning Production Run
DTSTART:20260415T060000Z
DTEND:20260415T180000Z
DESCRIPTION:Line 2 production
END:VEVENT
BEGIN:VEVENT
UID:evt-ics-002
SUMMARY:Planned shutdown
DTSTART:20260420T000000Z
DTEND:20260421T000000Z
END:VEVENT
END:VCALENDAR
"""

RECURRING_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:evt-recur-001
SUMMARY:Daily Standup
DTSTART:20260401T090000Z
DTEND:20260401T091500Z
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
END:VCALENDAR
"""


# ---------------------------------------------------------------------------
# CalendarConnector init & capabilities
# ---------------------------------------------------------------------------


class TestCalendarConnectorInit:
    """Test connector initialisation and backend selection."""

    def test_default_backend_is_ical(self) -> None:
        conn = CalendarConnector()
        assert conn._backend_name == "ical"

    def test_ical_capabilities_readonly(self) -> None:
        conn = CalendarConnector(backend="ical")
        assert conn.capabilities == ["read_calendar_events"]

    def test_google_capabilities_full(self) -> None:
        conn = CalendarConnector(backend="google")
        assert "create_calendar_event" in conn.capabilities
        assert "delete_calendar_event" in conn.capabilities

    def test_outlook_capabilities_full(self) -> None:
        conn = CalendarConnector(backend="outlook")
        assert "read_calendar_events" in conn.capabilities
        assert "create_calendar_event" in conn.capabilities

    def test_invalid_backend_raises(self) -> None:
        with pytest.raises(ConnectorError, match="Unknown calendar backend"):
            CalendarConnector(backend="yahoo")

    def test_calendar_type_map_forwarded(self) -> None:
        type_map = {"prod-cal": EventType.PRODUCTION}
        conn = CalendarConnector(backend="ical", calendar_type_map=type_map)
        assert conn._calendar_type_map == type_map


class TestCalendarConnectorNotConnected:
    """Test error handling when not connected."""

    @pytest.mark.asyncio
    async def test_read_events_not_connected(self) -> None:
        conn = CalendarConnector(backend="ical")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_events()

    @pytest.mark.asyncio
    async def test_create_event_not_connected(self) -> None:
        conn = CalendarConnector(backend="google")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.create_event(_make_event())

    @pytest.mark.asyncio
    async def test_delete_event_not_connected(self) -> None:
        conn = CalendarConnector(backend="google")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.delete_event("evt-1")


class TestCalendarConnectorHealthCheck:
    """Test health check behaviour."""

    @pytest.mark.asyncio
    async def test_unhealthy_when_not_connected(self) -> None:
        conn = CalendarConnector(backend="ical")
        health = await conn.health_check()
        assert health.status == "unhealthy"

    @pytest.mark.asyncio
    async def test_healthy_when_connected(self) -> None:
        conn = CalendarConnector(backend="ical")
        mock_backend = MagicMock()
        mock_backend.is_connected = True
        conn._backend = mock_backend
        health = await conn.health_check()
        assert health.status == "healthy"
        assert "ical" in health.message


# ---------------------------------------------------------------------------
# iCal backend
# ---------------------------------------------------------------------------


class TestICalBackend:
    """Test the iCal (.ics) backend."""

    pytestmark = pytest.mark.skipif(
        not importlib.util.find_spec("icalendar"),
        reason="icalendar not installed",
    )

    @pytest.mark.asyncio
    async def test_connect_no_source_raises(self) -> None:
        conn = CalendarConnector(backend="ical")
        with pytest.raises(ConnectorError, match="source is required"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        with patch.dict(sys.modules, {"icalendar": None}):
            conn = CalendarConnector(backend="ical", source="/fake.ics")
            with pytest.raises(ImportError, match="icalendar is required"):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_file_not_found(self) -> None:
        ical_mod = MagicMock()
        with patch.dict(sys.modules, {"icalendar": ical_mod}):
            conn = CalendarConnector(backend="ical", source="/nonexistent/path.ics")
            with pytest.raises(ConnectorError, match="iCal file not found"):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_parse_events_from_string(self) -> None:
        """Manually set raw data and parse events."""

        conn = CalendarConnector(backend="ical", source="dummy")
        # Build backend manually
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy")
        backend._raw_data = SAMPLE_ICS
        conn._backend = backend

        events = await conn.read_events()
        assert len(events) == 2
        assert events[0].title == "Morning Production Run"
        assert events[1].title == "Planned shutdown"

    @pytest.mark.asyncio
    async def test_parse_events_with_date_filter(self) -> None:
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy")
        backend._raw_data = SAMPLE_ICS
        conn = CalendarConnector(backend="ical", source="dummy")
        conn._backend = backend

        start = datetime(2026, 4, 19, tzinfo=UTC)
        end = datetime(2026, 4, 22, tzinfo=UTC)
        events = await conn.read_events(start=start, end=end)
        assert len(events) == 1
        assert events[0].id == "evt-ics-002"

    @pytest.mark.asyncio
    async def test_parse_recurring_events(self) -> None:
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy")
        backend._raw_data = RECURRING_ICS
        conn = CalendarConnector(backend="ical", source="dummy")
        conn._backend = backend

        start = datetime(2026, 4, 1, tzinfo=UTC)
        end = datetime(2026, 4, 10, tzinfo=UTC)
        events = await conn.read_events(start=start, end=end)
        assert len(events) == 5
        assert all(e.recurring is True for e in events)
        assert events[0].title == "Daily Standup"

    @pytest.mark.asyncio
    async def test_calendar_type_map_classification(self) -> None:
        from machina.connectors.calendar._ical import ICalBackend

        type_map = {"shutdown": EventType.DOWNTIME, "production": EventType.PRODUCTION}
        backend = ICalBackend(source="dummy", calendar_type_map=type_map)
        backend._raw_data = SAMPLE_ICS
        conn = CalendarConnector(backend="ical", source="dummy", calendar_type_map=type_map)
        conn._backend = backend

        events = await conn.read_events()
        # "Morning Production Run" → PRODUCTION, "Planned shutdown" → DOWNTIME
        assert events[0].event_type == EventType.PRODUCTION
        assert events[1].event_type == EventType.DOWNTIME

    @pytest.mark.asyncio
    async def test_create_event_raises_readonly(self) -> None:
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy")
        backend._raw_data = SAMPLE_ICS
        conn = CalendarConnector(backend="ical", source="dummy")
        conn._backend = backend

        with pytest.raises(ConnectorError, match="read-only"):
            await conn.create_event(_make_event())

    @pytest.mark.asyncio
    async def test_delete_event_raises_readonly(self) -> None:
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy")
        backend._raw_data = SAMPLE_ICS
        conn = CalendarConnector(backend="ical", source="dummy")
        conn._backend = backend

        with pytest.raises(ConnectorError, match="read-only"):
            await conn.delete_event("evt-1")

    @pytest.mark.asyncio
    async def test_disconnect_clears_data(self) -> None:
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy")
        backend._raw_data = SAMPLE_ICS
        conn = CalendarConnector(backend="ical", source="dummy")
        conn._backend = backend

        await conn.disconnect()
        assert conn._backend is None


# ---------------------------------------------------------------------------
# Google Calendar backend
# ---------------------------------------------------------------------------


class TestGoogleCalendarBackend:
    """Test the Google Calendar backend with mocked Google SDK."""

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        with patch.dict(sys.modules, {"googleapiclient": None, "googleapiclient.discovery": None}):
            conn = CalendarConnector(backend="google", service_account_file="/fake.json")
            with pytest.raises(ImportError, match="Google API libraries"):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_no_credentials_raises(self) -> None:
        # Mock the import so it doesn't fail
        mock_discovery = MagicMock()
        with patch.dict(
            sys.modules,
            {"googleapiclient": MagicMock(), "googleapiclient.discovery": mock_discovery},
        ):
            conn = CalendarConnector(backend="google")
            with pytest.raises(
                ConnectorAuthError, match="service_account_file or credentials_file"
            ):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_with_service_account(self) -> None:
        mock_discovery = MagicMock()
        mock_sa_mod = MagicMock()
        mock_creds = MagicMock()
        mock_sa_mod.Credentials.from_service_account_file.return_value = mock_creds
        mock_discovery.build.return_value = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "googleapiclient": MagicMock(),
                "googleapiclient.discovery": mock_discovery,
                "google": MagicMock(),
                "google.oauth2": MagicMock(),
                "google.oauth2.service_account": mock_sa_mod,
            },
        ):
            conn = CalendarConnector(backend="google", service_account_file="/sa.json")
            await conn.connect()
            assert conn._backend.is_connected

    @pytest.mark.asyncio
    async def test_list_events(self) -> None:
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "g-evt-1",
                    "summary": "Production Window",
                    "start": {"dateTime": "2026-04-15T06:00:00+00:00"},
                    "end": {"dateTime": "2026-04-15T18:00:00+00:00"},
                },
            ]
        }
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events

        from machina.connectors.calendar._google import GoogleCalendarBackend

        backend = GoogleCalendarBackend(
            service_account_file="/fake.json",
            calendar_type_map={"primary": EventType.PRODUCTION},
        )
        backend._service = mock_service

        conn = CalendarConnector(backend="google")
        conn._backend = backend

        events = await conn.read_events(calendar_id="primary")
        assert len(events) == 1
        assert events[0].id == "g-evt-1"
        assert events[0].event_type == EventType.PRODUCTION

    @pytest.mark.asyncio
    async def test_create_event(self) -> None:
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {
            "id": "g-created-1",
            "summary": "New Maintenance Window",
            "start": {"dateTime": "2026-04-20T08:00:00+00:00"},
            "end": {"dateTime": "2026-04-20T12:00:00+00:00"},
        }
        mock_events.insert.return_value = mock_insert
        mock_service.events.return_value = mock_events

        from machina.connectors.calendar._google import GoogleCalendarBackend

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        conn = CalendarConnector(backend="google")
        conn._backend = backend

        event = _make_event(title="New Maintenance Window")
        created = await conn.create_event(event, calendar_id="primary")
        assert created.id == "g-created-1"

    @pytest.mark.asyncio
    async def test_delete_event(self) -> None:
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_delete = MagicMock()
        mock_delete.execute.return_value = None
        mock_events.delete.return_value = mock_delete
        mock_service.events.return_value = mock_events

        from machina.connectors.calendar._google import GoogleCalendarBackend

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        conn = CalendarConnector(backend="google")
        conn._backend = backend

        await conn.delete_event("g-evt-1", calendar_id="primary")
        mock_events.delete.assert_called_once_with(calendarId="primary", eventId="g-evt-1")

    @pytest.mark.asyncio
    async def test_list_events_all_day(self) -> None:
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "g-allday",
                    "summary": "Plant Holiday",
                    "start": {"date": "2026-05-01"},
                    "end": {"date": "2026-05-02"},
                },
            ]
        }
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events

        from machina.connectors.calendar._google import GoogleCalendarBackend

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        conn = CalendarConnector(backend="google")
        conn._backend = backend

        events = await conn.read_events(calendar_id="primary")
        assert len(events) == 1
        assert events[0].all_day is True


# ---------------------------------------------------------------------------
# Outlook Calendar backend
# ---------------------------------------------------------------------------


class TestOutlookCalendarBackend:
    """Test the Outlook/Graph API backend with mocked MSAL and httpx."""

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        with patch.dict(sys.modules, {"msal": None}):
            conn = CalendarConnector(
                backend="outlook",
                tenant_id="t",
                client_id="c",
                client_secret="s",
            )
            with pytest.raises(ImportError, match="msal is required"):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_missing_credentials(self) -> None:
        mock_msal = MagicMock()
        with patch.dict(sys.modules, {"msal": mock_msal}):
            conn = CalendarConnector(backend="outlook")
            with pytest.raises(ConnectorAuthError, match="tenant_id, client_id"):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_token_error(self) -> None:
        mock_msal = MagicMock()
        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {
            "error_description": "Invalid client secret"
        }
        mock_msal.ConfidentialClientApplication.return_value = mock_app

        mock_httpx = MagicMock()
        with patch.dict(sys.modules, {"msal": mock_msal, "httpx": mock_httpx}):
            conn = CalendarConnector(
                backend="outlook",
                tenant_id="t",
                client_id="c",
                client_secret="bad",
            )
            with pytest.raises(ConnectorAuthError, match="Invalid client secret"):
                await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        mock_msal = MagicMock()
        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"access_token": "tok-123"}
        mock_msal.ConfidentialClientApplication.return_value = mock_app

        mock_httpx = MagicMock()
        with patch.dict(sys.modules, {"msal": mock_msal, "httpx": mock_httpx}):
            conn = CalendarConnector(
                backend="outlook",
                tenant_id="t",
                client_id="c",
                client_secret="s",
            )
            await conn.connect()
            assert conn._backend.is_connected

    @pytest.mark.asyncio
    async def test_list_events(self) -> None:
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            calendar_type_map={"cal-1": EventType.SHIFT},
        )

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "value": [
                {
                    "id": "o-evt-1",
                    "subject": "Morning Shift",
                    "start": {"dateTime": "2026-04-15T06:00:00"},
                    "end": {"dateTime": "2026-04-15T14:00:00"},
                    "isAllDay": False,
                },
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        backend._http = mock_http
        backend._token = "tok"
        mock_msal_app = MagicMock()
        mock_msal_app.acquire_token_silent.return_value = {"access_token": "tok"}
        backend._msal_app = mock_msal_app

        conn = CalendarConnector(backend="outlook")
        conn._backend = backend

        events = await conn.read_events(calendar_id="cal-1")
        assert len(events) == 1
        assert events[0].title == "Morning Shift"
        assert events[0].event_type == EventType.SHIFT

    @pytest.mark.asyncio
    async def test_create_event(self) -> None:
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "o-created-1",
            "subject": "New Event",
            "start": {"dateTime": "2026-04-20T08:00:00"},
            "end": {"dateTime": "2026-04-20T12:00:00"},
            "isAllDay": False,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        backend._http = mock_http
        backend._token = "tok"
        mock_msal_app = MagicMock()
        mock_msal_app.acquire_token_silent.return_value = {"access_token": "tok"}
        backend._msal_app = mock_msal_app

        conn = CalendarConnector(backend="outlook")
        conn._backend = backend

        event = _make_event(title="New Event")
        created = await conn.create_event(event)
        assert created.id == "o-created-1"

    @pytest.mark.asyncio
    async def test_delete_event(self) -> None:
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_http.delete = AsyncMock(return_value=mock_resp)
        backend._http = mock_http
        backend._token = "tok"
        mock_msal_app = MagicMock()
        mock_msal_app.acquire_token_silent.return_value = {"access_token": "tok"}
        backend._msal_app = mock_msal_app

        conn = CalendarConnector(backend="outlook")
        conn._backend = backend

        await conn.delete_event("o-evt-1")
        mock_http.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
        mock_http = AsyncMock()
        backend._http = mock_http
        backend._token = "tok"

        conn = CalendarConnector(backend="outlook")
        conn._backend = backend

        await conn.disconnect()
        assert conn._backend is None


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------


class TestConvenienceMethods:
    """Test the convenience query methods."""

    @pytest.mark.asyncio
    async def test_get_production_schedule(self) -> None:
        conn = CalendarConnector(backend="google")
        mock_backend = AsyncMock()
        mock_backend.is_connected = True
        mock_backend.list_events = AsyncMock(
            return_value=[
                _make_event(id="p-1", event_type=EventType.PRODUCTION),
                _make_event(id="m-1", event_type=EventType.MEETING),
                _make_event(id="p-2", event_type=EventType.PRODUCTION),
            ]
        )
        conn._backend = mock_backend

        result = await conn.get_production_schedule()
        assert len(result) == 2
        assert all(e.event_type == EventType.PRODUCTION for e in result)

    @pytest.mark.asyncio
    async def test_get_planned_downtime(self) -> None:
        conn = CalendarConnector(backend="google")
        mock_backend = AsyncMock()
        mock_backend.is_connected = True
        mock_backend.list_events = AsyncMock(
            return_value=[
                _make_event(id="d-1", event_type=EventType.DOWNTIME),
                _make_event(id="o-1", event_type=EventType.OTHER),
            ]
        )
        conn._backend = mock_backend

        result = await conn.get_planned_downtime()
        assert len(result) == 1
        assert result[0].event_type == EventType.DOWNTIME

    @pytest.mark.asyncio
    async def test_get_technician_availability(self) -> None:
        conn = CalendarConnector(backend="google")
        mock_backend = AsyncMock()
        mock_backend.is_connected = True
        mock_backend.list_events = AsyncMock(
            return_value=[
                _make_event(id="s-1", event_type=EventType.SHIFT),
                _make_event(id="s-2", event_type=EventType.SHIFT),
                _make_event(id="m-1", event_type=EventType.MAINTENANCE),
            ]
        )
        conn._backend = mock_backend

        result = await conn.get_technician_availability()
        assert len(result) == 2
        assert all(e.event_type == EventType.SHIFT for e in result)

    @pytest.mark.asyncio
    async def test_read_events_no_type_filter(self) -> None:
        conn = CalendarConnector(backend="google")
        mock_backend = AsyncMock()
        mock_backend.is_connected = True
        mock_backend.list_events = AsyncMock(
            return_value=[
                _make_event(id="a", event_type=EventType.PRODUCTION),
                _make_event(id="b", event_type=EventType.MEETING),
            ]
        )
        conn._backend = mock_backend

        result = await conn.read_events()
        assert len(result) == 2


# ---------------------------------------------------------------------------
# iCal backend — full coverage
# ---------------------------------------------------------------------------


SAMPLE_ICS_FULL = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:evt-1
SUMMARY:Production Run A
DTSTART:20260415T060000Z
DTEND:20260415T180000Z
DESCRIPTION:Main production window
LOCATION:Plant Floor A
END:VEVENT
BEGIN:VEVENT
UID:evt-2
SUMMARY:Maintenance Shutdown
DTSTART;VALUE=DATE:20260420
DTEND;VALUE=DATE:20260421
END:VEVENT
BEGIN:VEVENT
UID:evt-3
SUMMARY:Daily Standup
DTSTART:20260415T090000Z
DTEND:20260415T091500Z
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
BEGIN:VEVENT
UID:evt-no-end
SUMMARY:Single point event
DTSTART:20260416T100000Z
END:VEVENT
END:VCALENDAR
"""


class TestICalBackendFull:
    """Comprehensive tests for the iCal backend (requires icalendar installed)."""

    @pytest.mark.asyncio
    async def test_connect_and_list_events(self) -> None:
        """Full connect + list_events flow with real iCal parsing."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL

        events = await backend.list_events()
        # Should find at least 3 (evt-1, evt-2, evt-no-end; evt-3 recurring needs range)
        assert len(events) >= 3
        titles = {e.title for e in events}
        assert "Production Run A" in titles
        assert "Maintenance Shutdown" in titles

    @pytest.mark.asyncio
    async def test_all_day_event_parsing(self) -> None:
        """All-day events (DATE without time) are handled correctly."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL

        events = await backend.list_events()
        all_day = [e for e in events if e.title == "Maintenance Shutdown"]
        assert len(all_day) == 1
        assert all_day[0].all_day is True

    @pytest.mark.asyncio
    async def test_event_without_dtend(self) -> None:
        """Events without DTEND use DTSTART as fallback."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL

        events = await backend.list_events()
        no_end = [e for e in events if e.title == "Single point event"]
        assert len(no_end) == 1
        assert no_end[0].start == no_end[0].end

    @pytest.mark.asyncio
    async def test_date_range_filter(self) -> None:
        """Events outside the date range are excluded."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL

        start = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 15, 23, 59, tzinfo=UTC)
        events = await backend.list_events(start=start, end=end)
        # Only evt-1 and evt-no-end-ish fall in April 15 (+ recurring expansions)
        for e in events:
            assert e.start <= end
            assert e.end >= start

    @pytest.mark.asyncio
    async def test_recurring_event_expansion(self) -> None:
        """Recurring events are expanded within the date range."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL

        start = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
        events = await backend.list_events(start=start, end=end)
        standup_events = [e for e in events if "Standup" in e.title]
        # RRULE DAILY COUNT=5 starting April 15 → 5 occurrences, all within range
        assert len(standup_events) >= 3
        assert all(e.recurring is True for e in standup_events)

    @pytest.mark.asyncio
    async def test_calendar_type_map_classification(self) -> None:
        """Events are classified by calendar_type_map keywords."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(
            source="dummy.ics",
            calendar_type_map={
                "production": EventType.PRODUCTION,
                "maintenance": EventType.MAINTENANCE,
            },
        )
        backend._raw_data = SAMPLE_ICS_FULL

        events = await backend.list_events()
        prod_events = [e for e in events if e.event_type == EventType.PRODUCTION]
        maint_events = [e for e in events if e.event_type == EventType.MAINTENANCE]
        assert len(prod_events) >= 1
        assert len(maint_events) >= 1

    @pytest.mark.asyncio
    async def test_classify_event_default_other(self) -> None:
        """Unmatched events get EventType.OTHER."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL

        events = await backend.list_events()
        standup = [e for e in events if e.title == "Daily Standup"]
        if standup:
            assert standup[0].event_type == EventType.OTHER

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        """list_events raises when not connected."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        with pytest.raises(ConnectorError, match="Not connected"):
            await backend.list_events()

    @pytest.mark.asyncio
    async def test_connect_no_source_raises(self) -> None:
        """connect() raises when source is empty."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="")
        with pytest.raises(ConnectorError, match="source is required"):
            await backend.connect()

    @pytest.mark.asyncio
    async def test_connect_file_not_found_raises(self) -> None:
        """connect() raises when file does not exist."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="/nonexistent/path.ics")
        with pytest.raises(ConnectorError, match="not found"):
            await backend.connect()

    @pytest.mark.asyncio
    async def test_create_event_raises_readonly(self) -> None:
        """iCal backend refuses create_event."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        with pytest.raises(ConnectorError, match="read-only"):
            await backend.create_event("cal-1", _make_event())

    @pytest.mark.asyncio
    async def test_delete_event_raises_readonly(self) -> None:
        """iCal backend refuses delete_event."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        with pytest.raises(ConnectorError, match="read-only"):
            await backend.delete_event("cal-1", "evt-1")

    def test_is_connected(self) -> None:
        """is_connected reflects raw_data state."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        assert backend.is_connected is False
        backend._raw_data = "data"
        assert backend.is_connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        """disconnect clears cached data."""
        from machina.connectors.calendar._ical import ICalBackend

        backend = ICalBackend(source="dummy.ics")
        backend._raw_data = SAMPLE_ICS_FULL
        await backend.disconnect()
        assert backend._raw_data is None
        assert backend.is_connected is False


# ---------------------------------------------------------------------------
# Google Calendar — additional coverage
# ---------------------------------------------------------------------------


class TestGoogleCalendarBackendExtended:
    """Additional tests for uncovered Google Calendar paths."""

    @pytest.mark.asyncio
    async def test_connect_installed_app_flow(self) -> None:
        """Test installed-app OAuth2 flow (credentials_file)."""
        mock_flow_cls = MagicMock()
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = MagicMock()
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        mock_oauthlib = MagicMock()
        mock_oauthlib.flow.InstalledAppFlow = mock_flow_cls

        mock_build = MagicMock(return_value=MagicMock())

        with (
            patch.dict(
                sys.modules,
                {
                    "googleapiclient": MagicMock(),
                    "googleapiclient.discovery": MagicMock(build=mock_build),
                    "google_auth_oauthlib": mock_oauthlib,
                    "google_auth_oauthlib.flow": mock_oauthlib.flow,
                },
            ),
            patch("googleapiclient.discovery.build", mock_build),
        ):
            from machina.connectors.calendar._google import GoogleCalendarBackend

            backend = GoogleCalendarBackend(credentials_file="/fake/creds.json")
            await backend.connect()
            mock_flow_cls.from_client_secrets_file.assert_called_once()
            assert backend.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_no_credentials_raises(self) -> None:
        """connect() without credentials raises ConnectorAuthError."""
        mock_google = MagicMock()
        with patch.dict(
            sys.modules,
            {
                "googleapiclient": mock_google,
                "googleapiclient.discovery": mock_google.discovery,
            },
        ):
            from machina.connectors.calendar._google import GoogleCalendarBackend

            backend = GoogleCalendarBackend()
            with pytest.raises(ConnectorAuthError, match="credentials_file"):
                await backend.connect()

    @pytest.mark.asyncio
    async def test_delete_event(self) -> None:
        """delete_event calls the Google API."""
        from machina.connectors.calendar._google import GoogleCalendarBackend

        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_delete = MagicMock()
        mock_delete.execute.return_value = None
        mock_events.delete.return_value = mock_delete
        mock_service.events.return_value = mock_events

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        await backend.delete_event("primary", "evt-del-1")
        mock_events.delete.assert_called_once_with(calendarId="primary", eventId="evt-del-1")

    @pytest.mark.asyncio
    async def test_delete_event_not_connected_raises(self) -> None:
        """delete_event raises when not connected."""
        from machina.connectors.calendar._google import GoogleCalendarBackend

        backend = GoogleCalendarBackend()
        with pytest.raises(ConnectorError, match="Not connected"):
            await backend.delete_event("primary", "evt-1")

    @pytest.mark.asyncio
    async def test_create_event_all_day_with_attendees(self) -> None:
        """Create an all-day event with attendees and recurrence_rule."""
        from machina.connectors.calendar._google import GoogleCalendarBackend

        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {
            "id": "g-allday-new",
            "summary": "Plant Holiday",
            "start": {"date": "2026-05-01"},
            "end": {"date": "2026-05-02"},
        }
        mock_events.insert.return_value = mock_insert
        mock_service.events.return_value = mock_events

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        event = _make_event(
            title="Plant Holiday",
            all_day=True,
            attendees=["tech@example.com", "mgr@example.com"],
            recurrence_rule="RRULE:FREQ=YEARLY",
        )
        created = await backend.create_event("primary", event)
        assert created.id == "g-allday-new"

        # Verify insert was called
        mock_events.insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_events_with_date_filter(self) -> None:
        """list_events passes timeMin/timeMax when start/end are provided."""
        from machina.connectors.calendar._google import GoogleCalendarBackend

        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {"items": []}
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        start = datetime(2026, 4, 1, tzinfo=UTC)
        end = datetime(2026, 4, 30, tzinfo=UTC)
        await backend.list_events(start=start, end=end)

        call_kwargs = mock_events.list.call_args[1]
        assert "timeMin" in call_kwargs
        assert "timeMax" in call_kwargs

    @pytest.mark.asyncio
    async def test_event_with_attendees_and_recurrence(self) -> None:
        """Parsing an event with attendees and recurrence rules."""
        from machina.connectors.calendar._google import GoogleCalendarBackend

        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "g-recurring",
                    "summary": "Weekly Review",
                    "start": {"dateTime": "2026-04-15T10:00:00+00:00"},
                    "end": {"dateTime": "2026-04-15T11:00:00+00:00"},
                    "attendees": [
                        {"email": "a@test.com"},
                        {"email": "b@test.com"},
                    ],
                    "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=10"],
                    "recurringEventId": "g-master",
                },
            ]
        }
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events

        backend = GoogleCalendarBackend(service_account_file="/fake.json")
        backend._service = mock_service

        events = await backend.list_events()
        assert len(events) == 1
        assert events[0].attendees == ["a@test.com", "b@test.com"]
        assert events[0].recurring is True
        assert "FREQ=WEEKLY" in events[0].recurrence_rule


# ---------------------------------------------------------------------------
# Outlook Calendar — additional coverage
# ---------------------------------------------------------------------------


class TestOutlookCalendarBackendExtended:
    """Additional tests for uncovered Outlook paths."""

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        """Full connect flow with mocked MSAL and httpx."""
        mock_msal = MagicMock()
        mock_app = MagicMock()
        mock_app.acquire_token_for_client.return_value = {"access_token": "test-token"}
        mock_msal.ConfidentialClientApplication.return_value = mock_app

        mock_httpx = MagicMock()
        mock_client = AsyncMock()
        mock_httpx.AsyncClient.return_value = mock_client

        with patch.dict(sys.modules, {"msal": mock_msal, "httpx": mock_httpx}):
            from machina.connectors.calendar._outlook import OutlookCalendarBackend

            backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
            await backend.connect()
            assert backend.is_connected is True
            assert backend._msal_app is not None

    @pytest.mark.asyncio
    async def test_token_refresh_on_api_call(self) -> None:
        """Token is refreshed via _ensure_fresh_token before API calls."""
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": []}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.headers = {"Authorization": "Bearer old-token"}
        backend._http = mock_http
        backend._token = "old-token"

        # MSAL returns a new token
        mock_msal_app = MagicMock()
        mock_msal_app.acquire_token_silent.return_value = {"access_token": "new-token"}
        backend._msal_app = mock_msal_app

        await backend.list_events()
        assert backend._token == "new-token"
        assert mock_http.headers["Authorization"] == "Bearer new-token"

    @pytest.mark.asyncio
    async def test_token_silent_fallback_to_client_credentials(self) -> None:
        """When acquire_token_silent returns None, falls back to client credentials."""
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": []}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.headers = {"Authorization": "Bearer old"}
        backend._http = mock_http
        backend._token = "old"

        mock_msal_app = MagicMock()
        mock_msal_app.acquire_token_silent.return_value = None
        mock_msal_app.acquire_token_for_client.return_value = {"access_token": "fresh"}
        backend._msal_app = mock_msal_app

        await backend.list_events()
        assert backend._token == "fresh"
        mock_msal_app.acquire_token_for_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_events_default_calendar(self) -> None:
        """Empty calendar_id uses the user's default calendar URL."""
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": []}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        backend._http = mock_http
        backend._token = "tok"
        mock_msal = MagicMock()
        mock_msal.acquire_token_silent.return_value = {"access_token": "tok"}
        backend._msal_app = mock_msal

        await backend.list_events(calendar_id="")
        url_used = mock_http.get.call_args[0][0]
        assert "/calendar/events" in url_used
        assert "/calendars/" not in url_used

    @pytest.mark.asyncio
    async def test_list_events_with_date_filter(self) -> None:
        """Date filters produce $filter query parameters."""
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": []}
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        backend._http = mock_http
        backend._token = "tok"
        mock_msal = MagicMock()
        mock_msal.acquire_token_silent.return_value = {"access_token": "tok"}
        backend._msal_app = mock_msal

        start = datetime(2026, 4, 1, tzinfo=UTC)
        end = datetime(2026, 4, 30, tzinfo=UTC)
        await backend.list_events(start=start, end=end)

        call_kwargs = mock_http.get.call_args[1]
        assert "$filter" in call_kwargs.get("params", {})

    @pytest.mark.asyncio
    async def test_disconnect_clears_msal_app(self) -> None:
        """Disconnect clears MSAL app reference."""
        from machina.connectors.calendar._outlook import OutlookCalendarBackend

        backend = OutlookCalendarBackend(tenant_id="t", client_id="c", client_secret="s")
        backend._http = AsyncMock()
        backend._token = "tok"
        backend._msal_app = MagicMock()

        await backend.disconnect()
        assert backend._msal_app is None
        assert backend._token == ""
