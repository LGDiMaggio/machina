"""Workflow data models — triggers, steps, results, and error policies.

This module defines all the building blocks for composing and executing
multi-step maintenance workflows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------


class TriggerType(StrEnum):
    """Event types that can start a workflow."""

    ALARM = "alarm"
    SCHEDULE = "schedule"
    MANUAL = "manual"
    CONDITION = "condition"


class ErrorPolicy(StrEnum):
    """What to do when a step fails."""

    RETRY = "retry"
    SKIP = "skip"
    STOP = "stop"
    NOTIFY = "notify"


# ------------------------------------------------------------------
# Trigger
# ------------------------------------------------------------------


@dataclass
class Trigger:
    """Describes what event starts a workflow.

    Args:
        type: The kind of event (alarm, schedule, manual, condition).
        filter: Optional key-value filters to narrow matching events.
            For example ``{"severity": ["warning", "critical"]}`` means
            the workflow triggers only for those alarm severities.

    Example:
        ```python
        Trigger(type=TriggerType.ALARM, filter={"severity": ["critical"]})
        ```
    """

    type: TriggerType = TriggerType.MANUAL
    filter: dict[str, Any] = field(default_factory=dict)

    def matches(self, event: dict[str, Any]) -> bool:
        """Return True if *event* satisfies this trigger's filter.

        Each key in ``self.filter`` must be present in *event* and the
        event value must be contained in the filter's list of allowed
        values.  If **no filter** is set, every event matches.
        """
        for key, allowed in self.filter.items():
            value = event.get(key)
            if isinstance(allowed, list):
                if value not in allowed:
                    return False
            elif value != allowed:
                return False
        return True


# ------------------------------------------------------------------
# Guard condition
# ------------------------------------------------------------------


@dataclass
class GuardCondition:
    """A condition evaluated between steps to decide whether to proceed.

    Args:
        check: Callable that receives the current
            :class:`WorkflowContext` dict and returns ``True`` to proceed
            or ``False`` to skip the guarded step.
        description: Human-readable explanation of the guard.

    Example:
        ```python
        GuardCondition(
            check=lambda ctx: ctx.get("diagnose", {}).get("confidence") == "high",
            description="Only proceed if diagnosis confidence is high",
        )
        ```
    """

    check: Callable[[dict[str, Any]], bool] = field(default=lambda _ctx: True)
    description: str = ""


# ------------------------------------------------------------------
# Step
# ------------------------------------------------------------------


@dataclass
class Step:
    """A single step within a :class:`Workflow`.

    Each step maps to an action (a connector method, domain service call,
    or LLM reasoning invocation) and may carry a prompt template for
    LLM-powered steps.

    Args:
        name: Short identifier for the step (e.g. ``"diagnose_rules"``).
        action: Dot-path to the action (e.g. ``"cmms.read_work_orders"``).
        description: Human-readable explanation of what this step does.
        prompt: Optional prompt template for ``agent.reason`` steps.
            Placeholders in ``{step_name}`` format reference outputs
            from prior steps.
        template: Optional message template for notification steps.
        depends_on: List of step names whose output this step requires.
        on_error: What to do if this step fails.
        retries: Number of retry attempts (only meaningful when
            ``on_error`` is :attr:`ErrorPolicy.RETRY`).
        guard: Optional guard condition — if it returns ``False`` the
            step is skipped.
        timeout_seconds: Optional per-step timeout.
        inputs: Explicit input mappings using template variables, e.g.
            ``{"asset_id": "{trigger.asset_id}"}``.

    Example:
        ```python
        Step(
            "diagnose_rules",
            action="failure_analyzer.diagnose",
            description="Rule-based diagnosis using failure taxonomy",
            on_error=ErrorPolicy.RETRY,
            retries=2,
        )
        ```
    """

    name: str
    action: str = ""
    description: str = ""
    prompt: str = ""
    template: str = ""
    depends_on: list[str] = field(default_factory=list)
    on_error: ErrorPolicy = ErrorPolicy.STOP
    retries: int = 0
    guard: GuardCondition | None = None
    timeout_seconds: float | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    is_write: bool | None = None


# ------------------------------------------------------------------
# Workflow
# ------------------------------------------------------------------


@dataclass
class Workflow:
    """A named, trigger-driven sequence of :class:`Step` objects.

    Workflows define the complete pipeline for a maintenance process —
    from trigger event to final notification.

    Args:
        name: Human-readable workflow name.
        description: What this workflow does.
        trigger: Trigger specification or simple event-type string.
        steps: Ordered list of steps to execute.

    Example:
        ```python
        from machina.workflows import Step, Workflow, Trigger, TriggerType

        predictive = Workflow(
            name="Predictive Maintenance",
            trigger=Trigger(type=TriggerType.ALARM),
            steps=[
                Step("diagnose", action="failure_analyzer.diagnose"),
                Step("create_wo", action="work_order_factory.create"),
            ],
        )
        ```
    """

    name: str
    description: str = ""
    trigger: Trigger | str = ""
    steps: list[Step] = field(default_factory=list)

    @property
    def step_names(self) -> list[str]:
        """Return the ordered list of step names."""
        return [s.name for s in self.steps]

    def get_step(self, name: str) -> Step | None:
        """Look up a step by name, or ``None`` if not found."""
        for step in self.steps:
            if step.name == name:
                return step
        return None


# ------------------------------------------------------------------
# Execution results
# ------------------------------------------------------------------


@dataclass
class StepResult:
    """Outcome of a single step execution.

    Args:
        step_name: Name of the step that produced this result.
        output: Arbitrary output data returned by the step action.
        success: Whether the step completed without error.
        error: Error message if ``success`` is ``False``.
        duration_ms: Wall-clock time in milliseconds.
        skipped: Whether the step was skipped (guard or error policy).
    """

    step_name: str
    output: Any = None
    success: bool = True
    error: str | None = None
    duration_ms: float = 0.0
    skipped: bool = False


@dataclass
class WorkflowResult:
    """Outcome of a full workflow execution.

    Args:
        workflow_name: Name of the workflow.
        trigger_event: The event dict that started the workflow.
        step_results: Results for every step executed.
        success: ``True`` if all steps completed without fatal error.
        duration_ms: Total wall-clock time in milliseconds.
    """

    workflow_name: str
    trigger_event: dict[str, Any] = field(default_factory=dict)
    step_results: list[StepResult] = field(default_factory=list)
    success: bool = True
    duration_ms: float = 0.0


# ------------------------------------------------------------------
# Workflow context (used by the engine)
# ------------------------------------------------------------------


class WorkflowContext:
    """Mutable bag of data passed through a workflow execution.

    Stores the trigger event and the output of every completed step,
    keyed by step name.  Supports ``{step_name}`` and
    ``{step_name.field}`` template interpolation via :meth:`resolve`.
    """

    def __init__(self, trigger_event: dict[str, Any] | None = None) -> None:
        self._trigger: dict[str, Any] = trigger_event or {}
        self._steps: dict[str, Any] = {}

    # -- mutation ---------------------------------------------------

    def set_step_output(self, step_name: str, output: Any) -> None:
        """Record the output of a completed step."""
        self._steps[step_name] = output

    # -- read -------------------------------------------------------

    @property
    def trigger(self) -> dict[str, Any]:
        """The trigger event that started the workflow."""
        return self._trigger

    @property
    def steps(self) -> dict[str, Any]:
        """All step outputs collected so far."""
        return dict(self._steps)

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a value by step name, or return *default*."""
        return self._steps.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        """Flat dict with ``trigger`` and all step outputs."""
        return {"trigger": self._trigger, **self._steps}

    # -- template resolution ----------------------------------------

    def resolve(self, template: str) -> str:
        """Interpolate ``{key}`` placeholders in *template*.

        Supported patterns:

        * ``{trigger.field}`` — value from the trigger event.
        * ``{step_name}`` — full output of a prior step (str-ified).
        * ``{step_name.field}`` — nested field access (dict or object).

        Unknown placeholders are left as-is.
        """
        import re

        def _replacer(match: re.Match[str]) -> str:
            expr = match.group(1)
            parts = expr.split(".", 1)
            root = parts[0]

            if root == "trigger":
                if len(parts) == 1:
                    return str(self._trigger)
                return str(self._trigger.get(parts[1], match.group(0)))

            value = self._steps.get(root)
            if value is None:
                return match.group(0)  # leave unresolved

            if len(parts) == 1:
                return str(value)

            # nested access: dict key or attribute
            field_name = parts[1]
            if isinstance(value, dict):
                return str(value.get(field_name, match.group(0)))
            return str(getattr(value, field_name, match.group(0)))

        return re.sub(r"\{([^}]+)\}", _replacer, template)


__all__ = [
    "ErrorPolicy",
    "GuardCondition",
    "Step",
    "StepResult",
    "Trigger",
    "TriggerType",
    "Workflow",
    "WorkflowContext",
    "WorkflowResult",
]
