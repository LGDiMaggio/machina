"""Tests for the FailureMode domain entity."""

import pytest

from machina.domain.failure_mode import FailureMode


class TestFailureMode:
    """Test FailureMode creation and serialization."""

    def test_create_failure_mode(self, sample_failure_mode: FailureMode) -> None:
        assert sample_failure_mode.code == "BEAR-WEAR-01"
        assert sample_failure_mode.mechanism == "fatigue"
        assert sample_failure_mode.category == "mechanical"
        assert sample_failure_mode.mtbf_hours == 26000

    def test_detection_methods(self, sample_failure_mode: FailureMode) -> None:
        assert "vibration_analysis" in sample_failure_mode.detection_methods
        assert "temperature_monitoring" in sample_failure_mode.detection_methods

    def test_recommended_actions(self, sample_failure_mode: FailureMode) -> None:
        assert "replace_bearing" in sample_failure_mode.recommended_actions

    def test_minimal_failure_mode(self) -> None:
        fm = FailureMode(code="TEST-01", name="Test Failure")
        assert fm.code == "TEST-01"
        assert fm.mechanism == ""
        assert fm.mtbf_hours is None

    def test_serialization_roundtrip(self, sample_failure_mode: FailureMode) -> None:
        data = sample_failure_mode.model_dump()
        restored = FailureMode.model_validate(data)
        assert restored.code == sample_failure_mode.code
        assert restored.typical_indicators == sample_failure_mode.typical_indicators


class TestFailureModeValidation:
    """Test field validators."""

    def test_empty_code_rejected(self) -> None:
        with pytest.raises(ValueError, match="code cannot be empty"):
            FailureMode(code="", name="Bearing Wear")

    def test_code_stripped(self) -> None:
        fm = FailureMode(code="  BEAR-01  ", name="Bearing Wear")
        assert fm.code == "BEAR-01"
