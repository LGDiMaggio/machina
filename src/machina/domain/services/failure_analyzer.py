"""FailureAnalyzer — diagnose probable failure modes for an asset.

Given an asset's symptoms (alarms, sensor readings, technician observations),
the failure analyzer ranks the most likely failure modes and suggests
diagnostic steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
    from collections.abc import Iterator

    from machina.domain.alarm import Alarm
    from machina.domain.failure_mode import FailureMode


@dataclass
class DiagnosisResult:
    """Workflow-friendly wrapper around a ranked list of failure modes.

    Returned by :meth:`FailureAnalyzer.diagnose` when called from the
    workflow engine (i.e. with kwargs).  Carries the full ranked
    candidate list AND exposes a ``primary_code`` attribute that the
    workflow template engine can resolve via ``{analyze_alarm.primary_code}``
    — a plain ``str`` suitable for ``WorkOrder.failure_mode`` which
    requires ``str | None``.

    Iterates, indexes, and stringifies in ways the rest of the workflow
    (notification templates, audit logs) expects, so existing callers
    that treat the diagnosis as a list-of-dicts keep working.
    """

    matches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary(self) -> dict[str, Any] | None:
        """Top-ranked failure mode as a dict, or ``None`` when no match."""
        return self.matches[0] if self.matches else None

    @property
    def primary_code(self) -> str | None:
        """``str`` code of the top-ranked failure mode, suitable for ``WorkOrder.failure_mode``."""
        return self.matches[0]["code"] if self.matches else None

    @property
    def codes(self) -> list[str]:
        """All matching failure-mode codes, in rank order."""
        return [m["code"] for m in self.matches]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    def __bool__(self) -> bool:
        return bool(self.matches)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.matches[index]

    def __str__(self) -> str:
        if not self.matches:
            return "No matching failure modes"
        return ", ".join(
            f"{m.get('code', '?')} ({m.get('confidence', '?')})" for m in self.matches
        )


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

    @overload
    def diagnose(self, alarms: list[Alarm], **kwargs: Any) -> list[FailureMode]: ...

    @overload
    def diagnose(self, alarms: None = None, **kwargs: Any) -> DiagnosisResult: ...

    def diagnose(
        self,
        alarms: list[Alarm] | None = None,
        **kwargs: Any,
    ) -> list[FailureMode] | DiagnosisResult:
        """Return failure modes whose indicators match the alarm parameters.

        Can be called two ways:

        1. Programmatic: ``diagnose(alarms=[alarm1, alarm2])`` — returns
           a plain ``list[FailureMode]`` for Python callers.
        2. From workflow engine: ``diagnose(parameter="vibration_velocity_mm_s",
           value="7.8", asset_id="P-201", severity="warning")`` — returns
           a :class:`DiagnosisResult` so downstream workflow steps can
           extract a ``primary_code`` (``str``) for ``WorkOrder.failure_mode``
           via the template ``{analyze_alarm.primary_code}``.

        Args:
            alarms: Active alarms to match against failure modes.
            **kwargs: Workflow trigger fields (parameter, value, asset_id, severity).

        Returns:
            Either a list of matching :class:`FailureMode` objects
            (programmatic path) or a :class:`DiagnosisResult` (workflow
            path) — the workflow wrapper exposes the same ranked dicts
            via iteration plus convenience attributes for templating.
        """
        # Build the set of alarm parameters to match against
        if alarms:
            alarm_params = {a.parameter for a in alarms}
        elif "parameter" in kwargs:
            alarm_params = {kwargs["parameter"]}
        else:
            return DiagnosisResult() if kwargs and not alarms else []

        scored: list[tuple[int, Any]] = []
        for fm in self._failure_modes:
            overlap = alarm_params & set(fm.typical_indicators)
            if overlap:
                scored.append((len(overlap), fm))
        scored.sort(key=lambda x: x[0], reverse=True)

        # When called from workflow, return a DiagnosisResult so downstream
        # steps can resolve {analyze_alarm.primary_code} into a plain str
        # for WorkOrder.failure_mode (typed `str | None`).
        if kwargs and not alarms:
            matches = [
                {
                    "code": fm.code,
                    "name": fm.name,
                    "iso_14224_code": fm.iso_14224_code or "",
                    "category": fm.category,
                    "mechanism": fm.mechanism,
                    "matching_indicators": list(alarm_params & set(fm.typical_indicators)),
                    "recommended_actions": fm.recommended_actions,
                    # Confidence based on match ratio: what fraction of the
                    # failure mode's indicators are currently alarming.
                    "confidence": (
                        "high"
                        if score / len(fm.typical_indicators) >= 0.5
                        else "medium"
                        if score / len(fm.typical_indicators) >= 0.2
                        else "low"
                    ),
                }
                for score, fm in scored
            ]
            return DiagnosisResult(matches=matches)

        return [fm for _, fm in scored]
