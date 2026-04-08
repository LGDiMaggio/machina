"""End-to-end smoke test for the Knowledge Agent quickstart.

Verifies that ``examples/knowledge_agent/main.build_agent()`` constructs
a working Agent end-to-end: connectors wire up, the sample CMMS data
loads into the plant registry, and :meth:`Agent.start` succeeds with a
stub LLM.

Does NOT call :meth:`Agent.run` (would block on CliChannel stdin) and
does NOT call a real LLM. For a full request→response quickstart test
see the follow-up task D in the test-coverage plan.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAIN_PATH = REPO_ROOT / "examples" / "knowledge_agent" / "main.py"


def _load_knowledge_agent_main() -> ModuleType:
    """Dynamically import examples/knowledge_agent/main.py without
    turning the examples directory into a Python package."""
    spec = importlib.util.spec_from_file_location("knowledge_agent_main", MAIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_agent_main"] = module
    spec.loader.exec_module(module)
    return module


class _StubLLM:
    """LLMProvider-compatible stub — never makes a network call."""

    model = "stub:test"

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return "stub response"

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"content": "stub response", "tool_calls": None}


class TestKnowledgeAgentQuickstart:
    """MACHINA_SPEC R9 — the quickstart builds end-to-end with sample data."""

    def test_build_agent_constructs_successfully(self) -> None:
        """The quickstart builds without errors and wires up its connectors."""
        main_module = _load_knowledge_agent_main()
        agent = main_module.build_agent(llm=_StubLLM())
        assert agent.name == "Maintenance Knowledge Agent"
        # One channel (CliChannel by default)
        assert len(agent._channels) == 1
        # Two connectors: GenericCmmsConnector and DocumentStoreConnector
        registered = agent._registry.all()
        assert len(registered) == 2

    @pytest.mark.asyncio
    async def test_build_agent_start_loads_sample_assets(self) -> None:
        """agent.start() connects everything and loads the 6 sample assets."""
        main_module = _load_knowledge_agent_main()
        agent = main_module.build_agent(llm=_StubLLM())
        await agent.start()
        try:
            # 6 assets in examples/knowledge_agent/sample_data/cmms/assets.json
            assert len(agent.plant.assets) == 6
            assert "P-201" in agent.plant.assets
            p201 = agent.plant.assets["P-201"]
            # ISO 14224 field survived the load pipeline
            assert p201.equipment_class_code == "PU"
        finally:
            await agent.stop()
