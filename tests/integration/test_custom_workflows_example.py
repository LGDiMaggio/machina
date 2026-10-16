"""The custom-workflows reference example's reorder workflow, run on the sample data.

A workflow can report success while its steps do nothing: SKIP and NOTIFY
swallow a step that raises, and an unresolved placeholder renders verbatim.
So run the workflow for real, in sandbox mode, with the event ``run_demo()``
sends: every step must complete and every placeholder must resolve.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.workflows import WorkflowEngine

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_AGENT = REPO_ROOT / "examples" / "reference" / "custom_workflows" / "agent.py"
SAMPLE_CMMS_DIR = REPO_ROOT / "examples" / "sample_data" / "cmms"

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

    async def complete(self, messages: list[dict[str, str]]) -> str:
        raise AssertionError("sandbox mode must not call the LLM")


@pytest.mark.asyncio
async def test_reorder_workflow_runs_every_step_on_the_sample_data() -> None:
    example = _load_example()
    cmms = GenericCmmsConnector(data_dir=SAMPLE_CMMS_DIR)
    await cmms.connect()
    registry = ConnectorRegistry()
    registry.register("cmms", cmms)
    engine = WorkflowEngine(registry=registry, llm=_UncalledLLM(), sandbox=True)

    with capture_logs() as logs:
        result = await engine.execute(example.spare_part_reorder, dict(DEMO_EVENT))

    # A step that raised logs step_error; an unresolved placeholder logs
    # template_unresolved or input_unresolved. All three are warnings.
    assert [e for e in logs if e["log_level"] in {"warning", "error"}] == []
    assert [s.name for s in result.steps if s.skipped or not s.success] == []
    steps = {s.name: s for s in result.steps}
    assert [part.sku for part in steps["lookup_part"].output] == ["SKF-6310"]
