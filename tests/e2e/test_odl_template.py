"""End-to-end test of the odl-generator-from-text starter template.

Loads the template's own ``config.yaml`` through :meth:`Agent.from_config`
(the path ``agent.py`` takes), so the Excel substrate, the sample registry and
the YAML shape are exercised exactly as shipped. The LLM is a scripted stub —
no network — that turns a technician's message into two ``create_work_order``
tool calls; in sandbox mode both must be intercepted and nothing written.

Does NOT call :meth:`Agent.run` (would block on the CLI channel's stdin).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("openpyxl", reason="the template's registry is an .xlsx (machina-ai[excel])")

from machina import Agent

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "odl-generator-from-text"
MESSAGE = "pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"


class _ScriptedLLM:
    """Replays tool calls, one per turn iteration, then a final answer."""

    model = "stub:odl"

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = list(calls)

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Done."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        if not self._calls:
            return {"content": "Work orders proposed (sandbox).", "tool_calls": None}
        name, arguments = self._calls.pop(0)
        call = MagicMock()
        call.function.name = name
        call.function.arguments = json.dumps(arguments)
        call.id = f"call_{len(self._calls)}"
        return {"content": "", "tool_calls": [call]}


def _tool_results(agent: Agent, tool: str) -> list[dict[str, Any]]:
    return [
        json.loads(entry.metadata["result_json"])
        for entry in agent.tracer.entries
        if entry.action == "tool_call" and entry.operation == tool
    ]


@pytest.fixture()
def template_agent(monkeypatch: pytest.MonkeyPatch) -> Agent:
    # Sheet paths in config.yaml are relative to the template directory, as
    # agent.py arranges by switching into it.
    monkeypatch.chdir(TEMPLATE)
    monkeypatch.delenv("MACHINA_LLM_MODEL", raising=False)
    agent = Agent.from_config(TEMPLATE / "config.yaml")
    agent.sandbox = True
    return agent


@pytest.mark.asyncio
async def test_message_becomes_sandboxed_work_orders(template_agent: Agent) -> None:
    registry = json.loads((TEMPLATE / "data" / "asset_registry.json").read_text(encoding="utf-8"))
    template_agent._llm = _ScriptedLLM(  # type: ignore[assignment]
        [
            ("create_work_order", {"asset_id": "P-201", "description": "Perdita d'acqua"}),
            ("create_work_order", {"asset_id": "C-3", "description": "Rumore anomalo"}),
        ]
    )
    await template_agent.start()
    try:
        assert {a.id for a in template_agent.plant.assets.values()} == {a["id"] for a in registry}
        await template_agent.handle_message(MESSAGE)
    finally:
        await template_agent.stop()

    results = _tool_results(template_agent, "create_work_order")
    assert [r["args"]["asset_id"] for r in results] == ["P-201", "C-3"]
    assert all(r["sandbox"] is True for r in results)
    assert not (TEMPLATE / "data" / "workorders.xlsx").exists()


def test_rest_cmms_alternative_config_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.cmms-rest.yaml (the alternative substrate) loads and builds a
    Generic CMMS connector with the optional work-order and plan endpoints."""
    from machina.connectors.capabilities import Capability

    monkeypatch.chdir(TEMPLATE)
    monkeypatch.setenv("MACHINA_CMMS_API_KEY", "test-key")
    agent = Agent.from_config(TEMPLATE / "config.cmms-rest.yaml")
    ((_, cmms),) = [
        (name, conn)
        for name, conn in agent._registry.all().items()
        if not name.startswith("channel_")
    ]
    assert cmms.url == "http://localhost:9000"
    assert {
        Capability.GET_WORK_ORDER,
        Capability.UPDATE_WORK_ORDER,
        Capability.READ_MAINTENANCE_PLANS,
    } <= cmms.capabilities


@pytest.mark.asyncio
async def test_write_for_an_unresolved_asset_is_refused(template_agent: Agent) -> None:
    """The template inherits the resolution-authority gate: the turn named
    P-201 and C-3, so a work order for P-202 is refused, even in sandbox."""
    template_agent._llm = _ScriptedLLM(  # type: ignore[assignment]
        [("create_work_order", {"asset_id": "P-202", "description": "Not what was asked"})]
    )
    await template_agent.start()
    try:
        await template_agent.handle_message(MESSAGE)
    finally:
        await template_agent.stop()

    (result,) = _tool_results(template_agent, "create_work_order")
    assert "sandbox" not in result
    assert "P-202" not in json.dumps(result.get("args", {}))
