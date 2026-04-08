"""End-to-end smoke test for the Knowledge Agent quickstart.

Verifies that ``examples/knowledge_agent/main.build_agent()`` constructs
a working Agent end-to-end: connectors wire up, the sample CMMS data
loads into the plant registry, :meth:`Agent.start` succeeds with a stub
LLM, AND a grounded question actually retrieves the right asset context
and passes it to the LLM.

Does NOT call :meth:`Agent.run` (would block on CliChannel stdin) and
does NOT call a real LLM — uses capturing stubs to inspect the messages
that reach the provider.
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


class _CapturingStubLLM:
    """LLM stub that captures every messages list it receives.

    Lets tests inspect exactly what context reached the LLM, proving
    that retrieved entity metadata made it into the system prompt.
    """

    model = "stub:capture"

    def __init__(self) -> None:
        self.captured_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        self.captured_messages.append([dict(m) for m in messages])
        return "stub response (no tools)"

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.captured_messages.append([dict(m) for m in messages])
        return {"content": "stub response (with tools)", "tool_calls": None}


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

    @pytest.mark.asyncio
    async def test_quickstart_handles_grounded_question(self) -> None:
        """R9 grounding: ask about a real sample asset and verify the asset's
        metadata was actually injected into the LLM's system context.

        This is the full end-to-end quickstart path:
            user question -> entity resolver -> context gathering ->
            prompt building -> LLM invocation

        We use a capturing stub LLM so we can inspect exactly which messages
        reached the provider and assert the P-201 asset metadata is present.
        """
        main_module = _load_knowledge_agent_main()
        stub = _CapturingStubLLM()
        agent = main_module.build_agent(llm=stub)
        await agent.start()
        try:
            response = await agent.handle_message(
                "What is the status of pump P-201?",
                chat_id="test-chat",
            )

            # The stub LLM was actually invoked
            assert stub.captured_messages, "LLM was never called"
            assert "stub response" in response

            # The last LLM call carried the retrieved context with P-201's
            # metadata — this is what "grounded" means for R9
            last_call = stub.captured_messages[-1]
            system_content = " ".join(
                m.get("content", "") for m in last_call if m.get("role") == "system"
            )
            assert "P-201" in system_content, "P-201 asset ID missing from LLM system context"
            assert "Cooling Water Pump" in system_content, (
                "asset name missing from LLM system context"
            )
            assert "Grundfos" in system_content, "manufacturer missing from LLM system context"
        finally:
            await agent.stop()
