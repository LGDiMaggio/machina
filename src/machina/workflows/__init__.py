"""Workflow engine for multi-step maintenance processes.

Provides the :class:`Workflow` and :class:`Step` building blocks for
defining trigger-driven, multi-step maintenance workflows, and the
:class:`WorkflowEngine` for executing them.

A workflow is a named sequence of steps that can be attached to an
:class:`~machina.agent.runtime.Agent` and triggered by events (e.g. an
incoming alarm).  The engine resolves template variables between steps,
handles errors according to per-step policies, and traces every action
for observability.
"""

from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import (
    ErrorPolicy,
    GuardCondition,
    Step,
    StepResult,
    Trigger,
    TriggerType,
    Workflow,
    WorkflowContext,
    WorkflowResult,
)

__all__ = [
    "ErrorPolicy",
    "GuardCondition",
    "Step",
    "StepResult",
    "Trigger",
    "TriggerType",
    "Workflow",
    "WorkflowContext",
    "WorkflowEngine",
    "WorkflowResult",
]
