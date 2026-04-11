"""Tests for the Calendar domain entities."""

from datetime import UTC, datetime, time

import pytest

from machina.domain.calendar import (
    CalendarEvent,
    EventType,
    PlannedDowntime,
    ShiftPattern,
)

# ---------------------------------------------------------------------------
# EventType
# ---------------------------------------------------------------------------


class TestEventType:
    """Test EventType enum values."""

    def test_all_event_types(self) -> None:
        expected = {"production", "maintenance", "downtime", "shift", "meeting", "other"}
        assert {e.value for e in EventType} == expected

    def test_string_conversion(self) -> None:
        assert str(EventType.PRODUCTION) == "production"


# ---------------------------------------------------------------------------
# CalendarEvent
# ---------------------------------------------------------------------------


class TestCalendarEvent:
    """Test CalendarEvent creation and properties."""

    def test_create_event(self) -> None:
        event = CalendarEvent(
            id="evt-001",
            title="Line 2 Shutdown",
            start=datetime(2026, 4, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 4, 15, 18, 0, tzinfo=UTC),
            event_type=EventType.DOWNTIME,
            calendar_id="production-calendar",
        )
        assert event.id == "evt-001"
        assert event.title == "Line 2 Shutdown"
        assert event.event_type == EventType.DOWNTIME
        assert event.calendar_id == "production-calendar"

    def test_default_event_type(self) -> None:
        event = CalendarEvent(
            id="evt-002",
            title="Test",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert event.event_type == EventType.OTHER

    def test_duration_hours(self) -> None:
        event = CalendarEvent(
            id="evt-003",
            title="8h Window",
            start=datetime(2026, 4, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
        )
        assert event.duration_hours == 8.0

    def test_duration_hours_fractional(self) -> None:
        event = CalendarEvent(
            id="evt-004",
            title="90min Meeting",
            start=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
            end=datetime(2026, 4, 15, 11, 30, tzinfo=UTC),
        )
        assert event.duration_hours == 1.5

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id cannot be empty"):
            CalendarEvent(
                id="",
                title="Test",
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
            )

    def test_id_stripped(self) -> None:
        event = CalendarEvent(
            id="  evt-005  ",
            title="Test",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert event.id == "evt-005"

    def test_all_day_flag(self) -> None:
        event = CalendarEvent(
            id="evt-006",
            title="Holiday",
            start=datetime(2026, 5, 1, tzinfo=UTC),
            end=datetime(2026, 5, 2, tzinfo=UTC),
            all_day=True,
        )
        assert event.all_day is True
        assert event.duration_hours == 24.0

    def test_recurring_fields(self) -> None:
        event = CalendarEvent(
            id="evt-007",
            title="Daily Standup",
            start=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 4, 1, 9, 15, tzinfo=UTC),
            recurring=True,
            recurrence_rule="FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR",
        )
        assert event.recurring is True
        assert "FREQ=DAILY" in event.recurrence_rule

    def test_attendees_and_metadata(self) -> None:
        event = CalendarEvent(
            id="evt-008",
            title="Review",
            start=datetime(2026, 4, 10, 14, 0, tzinfo=UTC),
            end=datetime(2026, 4, 10, 15, 0, tzinfo=UTC),
            attendees=["mario.rossi", "luigi.bianchi"],
            metadata={"google_id": "abc123"},
        )
        assert len(event.attendees) == 2
        assert event.metadata["google_id"] == "abc123"

    def test_serialization_roundtrip(self) -> None:
        event = CalendarEvent(
            id="evt-009",
            title="Roundtrip Test",
            start=datetime(2026, 4, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 4, 15, 18, 0, tzinfo=UTC),
            event_type=EventType.PRODUCTION,
            attendees=["tech-1"],
        )
        data = event.model_dump()
        restored = CalendarEvent.model_validate(data)
        assert restored.id == event.id
        assert restored.event_type == event.event_type
        assert restored.attendees == event.attendees


# ---------------------------------------------------------------------------
# PlannedDowntime
# ---------------------------------------------------------------------------


class TestPlannedDowntime:
    """Test PlannedDowntime creation and properties."""

    def test_create_downtime(self) -> None:
        dt = PlannedDowntime(
            id="DT-001",
            area="Line 2",
            start=datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
            end=datetime(2026, 4, 21, 0, 0, tzinfo=UTC),
            reason="Annual turnaround",
        )
        assert dt.id == "DT-001"
        assert dt.area == "Line 2"
        assert dt.reason == "Annual turnaround"

    def test_duration_hours(self) -> None:
        dt = PlannedDowntime(
            id="DT-002",
            start=datetime(2026, 4, 20, 6, 0, tzinfo=UTC),
            end=datetime(2026, 4, 20, 18, 0, tzinfo=UTC),
        )
        assert dt.duration_hours == 12.0

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id cannot be empty"):
            PlannedDowntime(
                id="  ",
                start=datetime(2026, 4, 20, tzinfo=UTC),
                end=datetime(2026, 4, 21, tzinfo=UTC),
            )

    def test_optional_asset_id(self) -> None:
        dt = PlannedDowntime(
            id="DT-003",
            asset_id="P-201",
            start=datetime(2026, 4, 20, tzinfo=UTC),
            end=datetime(2026, 4, 21, tzinfo=UTC),
        )
        assert dt.asset_id == "P-201"

    def test_approved_default(self) -> None:
        dt = PlannedDowntime(
            id="DT-004",
            start=datetime(2026, 4, 20, tzinfo=UTC),
            end=datetime(2026, 4, 21, tzinfo=UTC),
        )
        assert dt.approved is False

    def test_serialization_roundtrip(self) -> None:
        dt = PlannedDowntime(
            id="DT-005",
            area="Utilities",
            start=datetime(2026, 4, 20, tzinfo=UTC),
            end=datetime(2026, 4, 21, tzinfo=UTC),
            reason="Boiler inspection",
            approved=True,
        )
        data = dt.model_dump()
        restored = PlannedDowntime.model_validate(data)
        assert restored.id == dt.id
        assert restored.approved is True


# ---------------------------------------------------------------------------
# ShiftPattern
# ---------------------------------------------------------------------------


class TestShiftPattern:
    """Test ShiftPattern creation and validation."""

    def test_create_shift(self) -> None:
        shift = ShiftPattern(
            id="SHIFT-AM",
            name="Morning Shift",
            start_time=time(6, 0),
            end_time=time(14, 0),
            days_of_week=[1, 2, 3, 4, 5],
        )
        assert shift.id == "SHIFT-AM"
        assert shift.name == "Morning Shift"
        assert shift.days_of_week == [1, 2, 3, 4, 5]

    def test_technicians_and_skills(self) -> None:
        shift = ShiftPattern(
            id="SHIFT-PM",
            name="Afternoon",
            start_time=time(14, 0),
            end_time=time(22, 0),
            days_of_week=[1, 2, 3, 4, 5],
            technicians=["mario", "luigi"],
            skills=["mechanical", "electrical"],
        )
        assert len(shift.technicians) == 2
        assert "electrical" in shift.skills

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id cannot be empty"):
            ShiftPattern(
                id="",
                name="Bad",
                start_time=time(6, 0),
                end_time=time(14, 0),
                days_of_week=[1],
            )

    def test_invalid_day_too_low(self) -> None:
        with pytest.raises(ValueError, match="day must be 1-7"):
            ShiftPattern(
                id="S-1",
                name="Bad Day",
                start_time=time(6, 0),
                end_time=time(14, 0),
                days_of_week=[0],
            )

    def test_invalid_day_too_high(self) -> None:
        with pytest.raises(ValueError, match="day must be 1-7"):
            ShiftPattern(
                id="S-2",
                name="Bad Day",
                start_time=time(6, 0),
                end_time=time(14, 0),
                days_of_week=[8],
            )

    def test_weekend_shift(self) -> None:
        shift = ShiftPattern(
            id="SHIFT-WE",
            name="Weekend",
            start_time=time(8, 0),
            end_time=time(16, 0),
            days_of_week=[6, 7],
        )
        assert shift.days_of_week == [6, 7]

    def test_serialization_roundtrip(self) -> None:
        shift = ShiftPattern(
            id="SHIFT-RT",
            name="Round-trip",
            start_time=time(6, 0),
            end_time=time(14, 0),
            days_of_week=[1, 2, 3],
            technicians=["tech-1"],
        )
        data = shift.model_dump()
        restored = ShiftPattern.model_validate(data)
        assert restored.id == shift.id
        assert restored.technicians == shift.technicians
