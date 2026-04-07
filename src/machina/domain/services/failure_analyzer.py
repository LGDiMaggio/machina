"""FailureAnalyzer — diagnose probable failure modes for an asset.

Given an asset's symptoms (alarms, sensor readings, technician observations),
the failure analyzer ranks the most likely failure modes and suggests
diagnostic steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from machina.domain.alarm import Alarm
    from machina.domain.failure_mode import FailureMode


class FailureAnalyzer:
    """Service that matches symptoms to known failure modes.

    This is a rule-based baseline; future versions will integrate
    LLM-assisted reasoning and historical failure data.
    """

    def __init__(self, failure_modes: list[FailureMode] | None = None) -> None:
        self._failure_modes = failure_modes or []

    def register_failure_mode(self, fm: FailureMode) -> None:
        """Add a failure mode to the knowledge base."""
        self._failure_modes.append(fm)

    def diagnose(self, alarms: list[Alarm]) -> list[FailureMode]:
        """Return failure modes whose indicators match any alarm parameter.

        Args:
            alarms: Active alarms to match against failure modes.

        Returns:
            List of potentially matching failure modes, ordered by
            number of matching indicators (descending).
        """
        alarm_params = {a.parameter for a in alarms}
        scored: list[tuple[int, FailureMode]] = []
        for fm in self._failure_modes:
            overlap = len(alarm_params & set(fm.typical_indicators))
            if overlap > 0:
                scored.append((overlap, fm))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [fm for _, fm in scored]
