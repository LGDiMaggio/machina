"""Tests for the Alarm domain entity."""

import pytest

from machina.domain.alarm import Alarm, Severity


class TestAlarm:
    """Test Alarm creation and threshold logic."""

    def test_create_alarm(self, sample_alarm: Alarm) -> None:
        assert sample_alarm.id == "ALM-2026-04-06-0847"
        assert sample_alarm.severity == Severity.WARNING
        assert sample_alarm.value == 7.8
        assert sample_alarm.threshold == 6.0

    def test_is_above_threshold(self, sample_alarm: Alarm) -> None:
        assert sample_alarm.is_above_threshold is True

    def test_not_above_threshold(self) -> None:
        alarm = Alarm(
            id="A-1",
            asset_id="P-1",
            severity=Severity.INFO,
            parameter="temp",
            value=50.0,
            threshold=80.0,
        )
        assert alarm.is_above_threshold is False

    def test_equal_to_threshold_is_not_above(self) -> None:
        alarm = Alarm(
            id="A-2",
            asset_id="P-1",
            severity=Severity.WARNING,
            parameter="temp",
            value=80.0,
            threshold=80.0,
        )
        assert alarm.is_above_threshold is False

    def test_acknowledged_default(self) -> None:
        alarm = Alarm(
            id="A-3",
            asset_id="P-1",
            severity=Severity.CRITICAL,
            parameter="pressure",
            value=12.0,
            threshold=10.0,
        )
        assert alarm.acknowledged is False

    def test_serialization_roundtrip(self, sample_alarm: Alarm) -> None:
        data = sample_alarm.model_dump()
        restored = Alarm.model_validate(data)
        assert restored.id == sample_alarm.id
        assert restored.value == sample_alarm.value


class TestSeverity:
    """Test Severity enum values."""

    def test_all_severities(self) -> None:
        expected = {"critical", "warning", "info"}
        assert {s.value for s in Severity} == expected


class TestAlarmValidation:
    """Test field validators."""

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id cannot be empty"):
            Alarm(
                id="",
                asset_id="P-1",
                severity=Severity.WARNING,
                parameter="temp",
                value=90.0,
                threshold=80.0,
            )

    def test_id_stripped(self) -> None:
        alarm = Alarm(
            id="  A-1  ",
            asset_id="P-1",
            severity=Severity.WARNING,
            parameter="temp",
            value=90.0,
            threshold=80.0,
        )
        assert alarm.id == "A-1"
