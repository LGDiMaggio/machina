"""The custom-workflows reference example's reorder workflow, run in sandbox mode.

A workflow can report success while its steps do nothing: SKIP and NOTIFY
swallow a step that raises, and an unresolved placeholder renders verbatim.
So run the workflow on the shipped sample data, read every step's outcome
and the notification it would send, and check what happens when the CMMS
has no part for the event.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms import GenericCmmsConnector
from machina.workflows import WorkflowEngine

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_AGENT = REPO_ROOT / "examples" / "reference" / "custom_workflows" / "agent.py"
SAMPLE_CMMS_DIR = REPO_ROOT / "examples" / "sample_data" / "cmms"

# The event run_demo() sends.
DEMO_EVENT = {
    "part_id": "SKF-6310",
    "current_stock": 1,
    "reorder_point": 2,
    "condition": "stock_below_reorder_point",
}


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

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise AssertionError("sandbox mode must not call the LLM")


def _warnings(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A step that raised logs step_error; an unresolved placeholder logs
    # template_unresolved or input_unresolved.
    return [e for e in logs if e["log_level"] in {"warning", "error"}]


async def _run_on_sample_cmms(event: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    cmms = GenericCmmsConnector(data_dir=SAMPLE_CMMS_DIR)
    await cmms.connect()
    registry = ConnectorRegistry()
    registry.register("cmms", cmms)
    engine = WorkflowEngine(registry=registry, llm=_UncalledLLM(), sandbox=True)
    with capture_logs() as logs:
        result = await engine.execute(_load_example().spare_part_reorder, event)
    return result, logs


@pytest.mark.asyncio
async def test_reorder_workflow_runs_every_step_on_the_sample_data() -> None:
    example = _load_example()
    agent = example.build_agent(llm=_UncalledLLM(), sandbox=True)
    await agent.start()
    try:
        with capture_logs() as logs:
            result = await agent.trigger_workflow(
                example.spare_part_reorder.name, dict(DEMO_EVENT)
            )
    finally:
        await agent.stop()

    assert _warnings(logs) == []
    assert result.success
    assert [(s.name, s.success, s.skipped, s.error) for s in result.steps] == [
        ("lookup_part", True, False, None),
        ("assess_urgency", True, False, None),
        ("place_order", True, False, None),
        ("notify_warehouse", True, False, None),
    ]
    steps = {s.name: s.output for s in result.steps}
    assert [part.sku for part in steps["lookup_part"]] == ["SKF-6310"]
    assert steps["place_order"]["__sandbox__"] is True
    # The part is named by the event's SKU, which the lookup just found in the CMMS.
    assert steps["notify_warehouse"]["message"] == (
        f"Spare Part Reorder\nPart: SKF-6310\nStock: 1\nAssessment: {steps['assess_urgency']}"
    )


@pytest.mark.asyncio
async def test_unknown_sku_is_not_assessed_ordered_or_announced() -> None:
    result, logs = await _run_on_sample_cmms({**DEMO_EVENT, "part_id": "NOT-IN-CMMS"})

    assert _warnings(logs) == []
    steps = {s.name: s for s in result.steps}
    assert steps["lookup_part"].success and steps["lookup_part"].output == []
    # Skipped by the guard, not after an error: nothing ran on the empty lookup.
    assert [(s.name, s.skipped, s.error, s.output) for s in result.steps[1:]] == [
        ("assess_urgency", True, None, None),
        ("place_order", True, None, None),
        ("notify_warehouse", True, None, None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {k: v for k, v in DEMO_EVENT.items() if k != "part_id"},
        {**DEMO_EVENT, "part_id": None},
        {**DEMO_EVENT, "part_id": ""},
        {**DEMO_EVENT, "part_id": "   "},
    ],
    ids=["absent", "none", "empty", "blank"],
)
async def test_event_without_a_part_id_stops_at_the_lookup(event: dict[str, Any]) -> None:
    # An empty sku is no filter: the read would return the whole inventory.
    result, _ = await _run_on_sample_cmms(event)

    assert not result.success
    assert [s.name for s in result.steps] == ["lookup_part"]
    error = result.steps[0].error
    assert error is not None and "needs its 'sku' input" in error
