"""U6 — Diagnosis-confidence gate (write-only accessor).

A low-confidence diagnosis must not be stamped onto a work order as fact. The
write-path accessor ``failure_mode_for_write`` returns ``None`` below medium
confidence, while the display accessors stay ungated. See the v0.3 plan.
"""

from __future__ import annotations

from machina.domain.services.failure_analyzer import DiagnosisResult


def _result(confidence: str) -> DiagnosisResult:
    return DiagnosisResult(
        matches=[{"code": "VIB", "name": "vibration", "confidence": confidence}]
    )


class TestFailureModeForWrite:
    def test_high_confidence_returns_code(self) -> None:
        assert _result("high").failure_mode_for_write == "VIB"

    def test_medium_confidence_returns_code(self) -> None:
        assert _result("medium").failure_mode_for_write == "VIB"

    def test_low_confidence_returns_none(self) -> None:
        assert _result("low").failure_mode_for_write is None

    def test_no_matches_returns_none(self) -> None:
        assert DiagnosisResult(matches=[]).failure_mode_for_write is None


class TestDisplayAccessorsStayUngated:
    def test_primary_code_returns_top_code_even_when_low(self) -> None:
        assert _result("low").primary_code == "VIB"

    def test_codes_returns_all_even_when_low(self) -> None:
        assert _result("low").codes == ["VIB"]
