"""FailureAnalyzer — diagnose probable failure modes for an asset.

Given an asset's symptoms (alarms, sensor readings, technician observations),
the failure analyzer ranks the most likely failure modes and suggests
diagnostic steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

    def diagnose(
        self,
        alarms: list[Alarm] | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Return failure modes whose indicators match the alarm parameters.

        Can be called two ways:

        1. Programmatic: ``diagnose(alarms=[alarm1, alarm2])``
        2. From workflow engine: ``diagnose(parameter="vibration_velocity_mm_s",
           value="7.8", asset_id="P-201", severity="warning")``

        Args:
            alarms: Active alarms to match against failure modes.
            **kwargs: Workflow trigger fields (parameter, value, asset_id, severity).

        Returns:
            List of matching failure modes with match details.
        """
        # Build the set of alarm parameters to match against
        if alarms:
            alarm_params = {a.parameter for a in alarms}
        elif "parameter" in kwargs:
            alarm_params = {kwargs["parameter"]}
        else:
            return []

        scored: list[tuple[int, Any]] = []
        for fm in self._failure_modes:
            overlap = alarm_params & set(fm.typical_indicators)
            if overlap:
                scored.append((len(overlap), fm))
        scored.sort(key=lambda x: x[0], reverse=True)

        # When called from workflow, return structured dicts for template resolution
        if kwargs and not alarms:
            return [
                {
                    "code": fm.code,
                    "name": fm.name,
                    "iso_14224_code": fm.iso_14224_code or "",
                    "category": fm.category,
                    "mechanism": fm.mechanism,
                    "matching_indicators": list(alarm_params & set(fm.typical_indicators)),
                    "recommended_actions": fm.recommended_actions,
                    "confidence": "high" if score >= 2 else "medium" if score == 1 else "low",
                }
                for score, fm in scored
            ]

        return [fm for _, fm in scored]
