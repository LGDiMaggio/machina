"""Workflow engine for multi-step maintenance processes.

Provides the :class:`Workflow` and :class:`Step` building blocks for
defining trigger-driven, multi-step maintenance workflows.  A workflow
is a named sequence of steps that can be attached to an
:class:`~machina.agent.runtime.Agent` and triggered by events (e.g. an
incoming alarm).

.. note::

    The workflow *execution engine* is not yet implemented — workflows can
    be defined and inspected but not automatically executed.  This is
    planned for Machina v0.2.  See ``MACHINA_SPEC.md`` Section 14.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    Example:
        ```python
        Step(
            "diagnose_rules",
            action="failure_analyzer.diagnose",
            description="Rule-based diagnosis using failure taxonomy",
        )
        ```
    """

    name: str
    action: str = ""
    description: str = ""
    prompt: str = ""
    template: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    """A named, trigger-driven sequence of :class:`Step` objects.

    Workflows define the complete pipeline for a maintenance process —
    from trigger event to final notification.

    Args:
        name: Human-readable workflow name.
        description: What this workflow does.
        trigger: Event type that starts the workflow (e.g. ``"alarm"``,
            ``"schedule"``, ``"manual"``).
        steps: Ordered list of steps to execute.

    Example:
        ```python
        from machina.workflows import Step, Workflow

        predictive = Workflow(
            name="Predictive Maintenance",
            trigger="alarm",
            steps=[
                Step("diagnose", action="failure_analyzer.diagnose"),
                Step("create_wo", action="work_order_factory.create"),
            ],
        )
        ```
    """

    name: str
    description: str = ""
    trigger: str = ""
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


__all__ = [
    "Step",
    "Workflow",
]
