"""The custom-workflows reference example's preventive scheduler, run in sandbox mode.

``notify_planners`` renders its message from the earlier steps' outputs. A
placeholder naming a field the output lacks stays in the text as written. One
naming a method of the output, such as ``{scan_plans.count}`` on a list,
renders the method's repr and logs no warning. So run the workflow and read
the message it would send.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from machina.domain.services.maintenance_scheduler import MaintenanceScheduler
from machina.domain.services.work_order_factory import WorkOrderFactory
from machina.workflows import WorkflowEngine

if TYPE_CHECKING:
    from types import ModuleType

    from machina.domain.maintenance_plan import MaintenancePlan

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_AGENT = REPO_ROOT / "examples" / "reference" / "custom_workflows" / "agent.py"


def _load_example() -> ModuleType:
    """Import the example under its own name — other tests import an ``agent`` module."""
    spec = importlib.util.spec_from_file_location("_custom_workflows_example", EXAMPLE_AGENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    path_snapshot = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        # The module body prepends the repo's src/ and examples/ to sys.path.
        sys.path[:] = path_snapshot
    return module


class _UncalledLLM:
    """``agent.reason`` needs an LLM, which sandbox mode never calls."""

    async def complete(self, messages: list[dict[str, str]]) -> str:
        raise AssertionError("sandbox mode must not call the LLM")


@pytest.mark.asyncio
async def test_scheduler_workflow_runs_every_step_and_fills_its_notification(
    sample_maintenance_plan: MaintenancePlan,
) -> None:
    workflow = _load_example().preventive_scheduling
    # Agent.start() registers these services, handing the scheduler the plans
    # its connectors loaded. The sample CMMS data has none, so pass one here.
    engine = WorkflowEngine(
        llm=_UncalledLLM(),
        services={
            "maintenance_scheduler": MaintenanceScheduler(plans=[sample_maintenance_plan]),
            "work_order_factory": WorkOrderFactory(),
        },
        sandbox=True,
    )

    with capture_logs() as logs:
        result = await engine.execute(workflow)

    assert [s.name for s in result.steps if s.skipped or not s.success] == []
    steps = {s.name: s for s in result.steps}
    assert [plan["plan_id"] for plan in steps["scan_plans"].output] == ["MP-P201-QUARTERLY"]

    message = steps["notify_planners"].output["message"]
    # A method reached through {step.field} renders its repr, and nothing is logged.
    assert "<built-in method" not in message
    template = next(s.template for s in workflow.steps if s.name == "notify_planners")
    assert [p for p in re.findall(r"\{[^}]+\}", template) if p in message] == []
    # An unresolved placeholder also logs template_unresolved, a warning.
    assert [e for e in logs if e["log_level"] in {"warning", "error"}] == []
