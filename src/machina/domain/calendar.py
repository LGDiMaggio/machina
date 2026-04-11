"""Calendar domain entities — events, shifts, and planned downtime.

Provides typed representations for calendar events from production
schedules, shift rosters, and maintenance windows.  Used by the
:class:`~machina.connectors.calendar.CalendarConnector` to normalise
data from Google Calendar, Outlook, and iCal sources.
"""

from __future__ import annotations

from datetime import datetime, time  # noqa: TC003 — pydantic needs these at runtime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class EventType(StrEnum):
    """Classification of a calendar event in the maintenance domain."""

    PRODUCTION = "production"
    MAINTENANCE = "maintenance"
    DOWNTIME = "downtime"
    SHIFT = "shift"
    MEETING = "meeting"
    OTHER = "other"


class CalendarEvent(BaseModel):
    """A calendar event from any backend (Google, Outlook, iCal).

    Represents a single occurrence — recurring events are expanded into
    individual ``CalendarEvent`` instances by the connector.

    Example:
        ```python
        from datetime import datetime, timezone
        from machina.domain.calendar import CalendarEvent, EventType

        event = CalendarEvent(
            id="evt-001",
            title="Line 2 Planned Shutdown",
            start=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc),
            end=datetime(2026, 4, 15, 18, 0, tzinfo=timezone.utc),
            event_type=EventType.DOWNTIME,
            calendar_id="production-calendar",
        )
        ```
    """

    id: str = Field(..., description="Unique event identifier")
    title: str = Field(..., description="Event title / summary")
    start: datetime = Field(..., description="Event start (timezone-aware)")
    end: datetime = Field(..., description="Event end (timezone-aware)")
    event_type: EventType = Field(
        default=EventType.OTHER, description="Maintenance-domain classification"
    )
    calendar_id: str = Field(default="", description="Source calendar identifier")
    description: str = Field(default="", description="Event description / notes")
    location: str = Field(default="", description="Event location (e.g. plant area)")
    all_day: bool = Field(default=False, description="Whether this is an all-day event")
    recurring: bool = Field(
        default=False, description="Whether this event was expanded from a recurrence"
    )
    recurrence_rule: str = Field(default="", description="RRULE string (only on the master event)")
    attendees: list[str] = Field(default_factory=list, description="Attendee emails or names")
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Backend-specific extra fields",
    )

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id cannot be empty")
        return v.strip()

    @property
    def duration_hours(self) -> float:
        """Duration of the event in hours."""
        delta = self.end - self.start
        return delta.total_seconds() / 3600


class PlannedDowntime(BaseModel):
    """A scheduled downtime window for an asset or plant area.

    Represents planned production stops, turnarounds, or maintenance
    windows extracted from calendar data.

    Example:
        ```python
        from datetime import datetime, timezone
        from machina.domain.calendar import PlannedDowntime

        dt = PlannedDowntime(
            id="DT-2026-04",
            area="Line 2",
            start=datetime(2026, 4, 20, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc),
            reason="Annual turnaround",
        )
        ```
    """

    id: str = Field(..., description="Downtime identifier")
    asset_id: str = Field(default="", description="Related asset (empty = whole area)")
    area: str = Field(default="", description="Plant area affected")
    start: datetime = Field(..., description="Downtime start")
    end: datetime = Field(..., description="Downtime end")
    reason: str = Field(default="", description="Reason for the downtime")
    approved: bool = Field(default=False, description="Whether downtime is approved")

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id cannot be empty")
        return v.strip()

    @property
    def duration_hours(self) -> float:
        """Duration of the downtime in hours."""
        return (self.end - self.start).total_seconds() / 3600


class ShiftPattern(BaseModel):
    """A recurring shift schedule defining technician availability.

    Days of week use ISO convention: 1 = Monday … 7 = Sunday.

    Example:
        ```python
        from datetime import time
        from machina.domain.calendar import ShiftPattern

        morning = ShiftPattern(
            id="SHIFT-MORNING",
            name="Morning Shift",
            start_time=time(6, 0),
            end_time=time(14, 0),
            days_of_week=[1, 2, 3, 4, 5],
            technicians=["mario.rossi", "luigi.bianchi"],
            skills=["mechanical", "electrical"],
        )
        ```
    """

    id: str = Field(..., description="Shift pattern identifier")
    name: str = Field(..., description="Human-readable shift name")
    start_time: time = Field(..., description="Shift start time (local)")
    end_time: time = Field(..., description="Shift end time (local)")
    days_of_week: list[int] = Field(
        ...,
        description="Active days (ISO: 1=Mon … 7=Sun)",
    )
    technicians: list[str] = Field(
        default_factory=list,
        description="Technician IDs / names assigned to this shift",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Skills available during this shift",
    )

    model_config = {"frozen": False, "str_strip_whitespace": True}

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id cannot be empty")
        return v.strip()

    @field_validator("days_of_week")
    @classmethod
    def _validate_days(cls, v: list[int]) -> list[int]:
        for day in v:
            if day < 1 or day > 7:
                raise ValueError(f"day must be 1-7 (ISO weekday), got {day}")
        return v
