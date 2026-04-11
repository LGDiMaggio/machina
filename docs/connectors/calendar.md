# Calendar Connector

The `CalendarConnector` provides access to production schedules, shift patterns, technician availability, and planned downtime windows. It supports three pluggable backends behind a unified interface.

| Backend | Source | Read | Write |
|---------|--------|------|-------|
| **iCal** | `.ics` files or URLs | ✅ | ❌ (read-only) |
| **Google Calendar** | Google Calendar API v3 | ✅ | ✅ |
| **Outlook** | Microsoft Graph API | ✅ | ✅ |

## Prerequisites

=== "iCal"

    - An `.ics` file (local path or HTTP/HTTPS URL)
    - No external service credentials needed

=== "Google Calendar"

    - A Google Cloud project with Calendar API enabled
    - A service account key file **or** OAuth2 client secrets
    - Shared calendars must grant the service account read/write access

=== "Outlook"

    - An Azure AD app registration with `Calendars.Read` and `Calendars.ReadWrite` application permissions
    - Admin consent granted for the tenant
    - Tenant ID, Client ID, and Client Secret

## Installation

```bash
# iCal backend (lightweight)
pip install machina-ai[ical]

# Google Calendar backend
pip install machina-ai[google-calendar]

# Outlook / Microsoft 365 backend
pip install machina-ai[outlook-calendar]

# All three backends
pip install machina-ai[calendar]
```

## Quick Start

=== "iCal"

    ```python
    from machina.connectors import Calendar
    from machina.domain.calendar import EventType

    cal = Calendar(
        backend="ical",
        source="/data/production_schedule.ics",
        calendar_type_map={
            "production": EventType.PRODUCTION,
            "shutdown": EventType.DOWNTIME,
        },
    )

    await cal.connect()
    events = await cal.read_events()
    ```

=== "Google Calendar"

    ```python
    from machina.connectors import Calendar
    from machina.domain.calendar import EventType

    cal = Calendar(
        backend="google",
        service_account_file="/secrets/sa-key.json",
        calendar_type_map={
            "prod-calendar-id@group.calendar.google.com": EventType.PRODUCTION,
            "shifts-calendar-id@group.calendar.google.com": EventType.SHIFT,
        },
    )

    await cal.connect()
    events = await cal.read_events(calendar_id="prod-calendar-id@group.calendar.google.com")
    ```

=== "Outlook"

    ```python
    from machina.connectors import Calendar
    from machina.domain.calendar import EventType

    cal = Calendar(
        backend="outlook",
        tenant_id="your-tenant-id",
        client_id="your-client-id",
        client_secret="your-client-secret",
        user_id="plant-ops@company.com",
        calendar_type_map={
            "AAMkAG...": EventType.PRODUCTION,  # Outlook calendar ID
        },
    )

    await cal.connect()
    events = await cal.read_events(calendar_id="AAMkAG...")
    ```

## YAML Configuration

```yaml
connectors:
  calendar:
    type: calendar
    settings:
      backend: ical
      source: /data/schedules/production.ics
      calendar_type_map:
        production: production
        shutdown: downtime
        shift: shift
```

## Capabilities

| Capability | iCal | Google | Outlook | Description |
|-----------|------|--------|---------|-------------|
| `read_calendar_events` | ✅ | ✅ | ✅ | List and filter calendar events |
| `create_calendar_event` | ❌ | ✅ | ✅ | Create a new event |
| `delete_calendar_event` | ❌ | ✅ | ✅ | Delete an existing event |

## Calendar Type Mapping

The `calendar_type_map` parameter maps calendar identifiers to `EventType` values for automatic classification:

- **iCal**: Maps **summary keywords** — if the keyword appears in an event's title, it's classified with that type
- **Google/Outlook**: Maps **calendar IDs** — all events from a calendar inherit its type

```python
from machina.domain.calendar import EventType

# iCal: keyword-based mapping
cal = Calendar(
    backend="ical",
    source="schedules.ics",
    calendar_type_map={
        "production": EventType.PRODUCTION,
        "shutdown": EventType.DOWNTIME,
        "shift": EventType.SHIFT,
    },
)

# Google: calendar-ID-based mapping
cal = Calendar(
    backend="google",
    service_account_file="sa.json",
    calendar_type_map={
        "prod-cal@group.calendar.google.com": EventType.PRODUCTION,
        "shifts-cal@group.calendar.google.com": EventType.SHIFT,
    },
)
```

Available event types: `production`, `maintenance`, `downtime`, `shift`, `meeting`, `other`.

## Convenience Methods

The connector provides helper methods that filter events by type:

```python
from datetime import datetime, timezone

start = datetime(2026, 4, 1, tzinfo=timezone.utc)
end = datetime(2026, 4, 30, tzinfo=timezone.utc)

# Production windows
schedule = await cal.get_production_schedule(start=start, end=end)

# Planned downtime / shutdowns
downtime = await cal.get_planned_downtime(start=start, end=end)

# Technician shift patterns
shifts = await cal.get_technician_availability(start=start, end=end)
```

## Domain Entities

The connector returns `CalendarEvent` instances and also provides related domain models:

- **`CalendarEvent`** — A single calendar event with `id`, `title`, `start`, `end`, `event_type`, `attendees`, and metadata
- **`PlannedDowntime`** — A typed downtime window with `asset_id`, `area`, `reason`, and `approved` fields
- **`ShiftPattern`** — A recurring shift definition with `start_time`, `end_time`, `days_of_week`, `technicians`, and `skills`

## Recurring Events

The iCal backend automatically expands recurring events (RRULE) within the requested time range. Google and Outlook APIs handle expansion server-side when `singleEvents=True`.

Maximum 500 occurrences are returned per recurring event to prevent memory issues.

## Limitations

- **iCal backend is read-only** — creating or deleting events raises `ConnectorError`
- **iCal URL fetching** uses Python's `urllib` with a 30-second timeout
- **Google Calendar** service account requires explicit calendar sharing
- **Outlook** requires Azure AD application permissions with admin consent
- **Recurring event expansion** is capped at 500 occurrences per master event
- Time zones: all events are normalised to UTC-aware datetimes

## API Reference

::: machina.connectors.calendar.connector.CalendarConnector
