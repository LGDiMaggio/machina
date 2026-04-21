"""End-to-end smoke test for the quickstart example.

Verifies that the quickstart agent constructs correctly, loads sample data,
and passes grounded context to the LLM.

Does NOT call :meth:`Agent.run` (would block on CliChannel stdin) and
does NOT call a real LLM — uses capturing stubs to inspect the messages
that reach the provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DIR = REPO_ROOT / "examples" / "sample_data"


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
    """LLM stub that captures every messages list it receives."""

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


def _build_agent(llm: Any = None) -> Agent:
    """Build the quickstart agent with sample data and an optional LLM override."""
    return Agent(
        name="Quickstart Test Agent",
        plant=Plant(name="Demo Plant"),
        connectors=[
            GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
            DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
        ],
        channels=[CliChannel()],
        llm=llm or _StubLLM(),
    )


class TestQuickstartAgent:
    """MACHINA_SPEC R9 — the quickstart builds end-to-end with sample data."""

    def test_build_agent_constructs_successfully(self) -> None:
        """The quickstart builds without errors and wires up its connectors."""
        agent = _build_agent()
        assert agent.name == "Quickstart Test Agent"
        assert len(agent._channels) == 1
        # Non-channel connectors only — channels are also in the registry now
        # under keys prefixed with "channel_" (see issue #31).
        non_channel = {
            k: v for k, v in agent._registry.all().items() if not k.startswith("channel_")
        }
        assert len(non_channel) == 2

    @pytest.mark.asyncio
    async def test_build_agent_start_loads_sample_assets(self) -> None:
        """agent.start() connects everything and loads the 6 sample assets."""
        agent = _build_agent()
        await agent.start()
        try:
            assert len(agent.plant.assets) == 6
            assert "P-201" in agent.plant.assets
            p201 = agent.plant.assets["P-201"]
            assert p201.equipment_class_code == "PU"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_start_loads_failure_modes_and_domain_services(self) -> None:
        """agent.start() wires domain services with failure modes from CMMS."""
        agent = _build_agent()
        await agent.start()
        try:
            services = agent._engine._services
            assert "failure_analyzer" in services
            assert "work_order_factory" in services
            assert "maintenance_scheduler" in services

            analyzer = services["failure_analyzer"]
            assert len(analyzer._failure_modes) == 10

            # Vibration alarm should match bearing wear
            result = analyzer.diagnose(parameter="vibration_velocity_mm_s")
            codes = [r["code"] for r in result]
            assert "BEAR-WEAR-01" in codes
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_quickstart_handles_grounded_question(self) -> None:
        """R9 grounding: ask about a real sample asset and verify the asset's
        metadata was actually injected into the LLM's system context."""
        stub = _CapturingStubLLM()
        agent = _build_agent(llm=stub)
        await agent.start()
        try:
            response = await agent.handle_message(
                "What is the status of pump P-201?",
                chat_id="test-chat",
            )

            assert stub.captured_messages, "LLM was never called"
            assert "stub response" in response

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
