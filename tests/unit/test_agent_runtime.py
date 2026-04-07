"""Tests for the Agent runtime."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina.agent.runtime import Agent
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plant() -> Plant:
    """Create a plant with one asset for testing."""
    plant = Plant(name="Test Plant")
    plant.register_asset(
        Asset(
            id="P-201",
            name="Cooling Water Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building A",
            criticality=Criticality.A,
        )
    )
    return plant


class _FakeConnector:
    """Minimal connector stub implementing the Protocol."""

    capabilities: ClassVar[list[str]] = ["read_assets", "read_work_orders"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def read_assets(self, **kwargs: Any) -> list[Asset]:
        return [
            Asset(
                id="P-201",
                name="Cooling Water Pump",
                type=AssetType.ROTATING_EQUIPMENT,
                location="Building A",
                criticality=Criticality.A,
            ),
        ]

    async def read_work_orders(self, **kwargs: Any) -> list[WorkOrder]:
        return [
            WorkOrder(
                id="WO-001",
                type=WorkOrderType.CORRECTIVE,
                priority=Priority.HIGH,
                asset_id="P-201",
                description="Replace bearing",
            ),
        ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentInit:
    """Test Agent construction."""

    def test_defaults(self) -> None:
        agent = Agent()
        assert agent.name == "Machina Agent"
        assert agent.plant is not None

    def test_custom_plant(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        assert agent.plant.name == "Test Plant"

    def test_with_connectors(self) -> None:
        agent = Agent(connectors=[_FakeConnector()])
        assert len(agent._registry.all()) == 1

    def test_string_llm(self) -> None:
        agent = Agent(llm="openai:gpt-4o")
        assert agent._llm.model == "openai:gpt-4o"


class TestAgentStart:
    """Test agent startup — connects connectors and loads assets."""

    @pytest.mark.asyncio
    async def test_start_connects_and_loads(self) -> None:
        conn = _FakeConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        # Assets should have been loaded from the connector
        assert len(agent.plant.assets) >= 1
        assert agent.plant.get_asset("P-201") is not None

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        conn = _FakeConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        await agent.stop()  # Should not raise


class TestToolSearch:
    """Test _tool_search_assets and _tool_get_asset_details."""

    def test_search_assets(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        results = agent._tool_search_assets("P-201")
        assert len(results) == 1
        assert results[0]["id"] == "P-201"

    def test_search_assets_no_match(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        results = agent._tool_search_assets("nonexistent")
        assert isinstance(results, list)

    def test_get_asset_details(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        details = agent._tool_get_asset_details("P-201")
        assert details["id"] == "P-201"
        assert details["name"] == "Cooling Water Pump"

    def test_get_asset_details_not_found(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        result = agent._tool_get_asset_details("NOPE")
        assert "error" in result


class TestExecuteTool:
    """Test _execute_tool dispatching."""

    @pytest.mark.asyncio
    async def test_search_assets_tool(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        result = await agent._execute_tool("search_assets", {"query": "P-201"})
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_unknown_tool(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("nonexistent_tool", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_read_work_orders_tool(self) -> None:
        conn = _FakeConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        result = await agent._execute_tool(
            "read_work_orders", {"asset_id": "P-201"}
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_read_work_orders_no_connector(self) -> None:
        agent = Agent()
        result = await agent._execute_tool(
            "read_work_orders", {"asset_id": "P-201"}
        )
        assert "error" in result


class TestAvailableTools:
    """Test _get_available_tools."""

    def test_with_connector(self) -> None:
        conn = _FakeConnector()
        agent = Agent(connectors=[conn])
        tools = agent._get_available_tools()
        names = {t["function"]["name"] for t in tools}
        # Connector has read_assets + read_work_orders
        assert "search_assets" in names
        assert "get_asset_details" in names
        assert "read_work_orders" in names
        # Always-on tools
        assert "diagnose_failure" in names

    def test_no_connectors(self) -> None:
        agent = Agent()
        tools = agent._get_available_tools()
        names = {t["function"]["name"] for t in tools}
        # Only always-on tools
        assert "diagnose_failure" in names
        assert "get_maintenance_schedule" in names
        # Should NOT include connector-dependent tools
        assert "search_assets" not in names


class TestHistory:
    """Test conversation history management."""

    def test_add_to_history(self) -> None:
        agent = Agent(max_history=5)
        agent._add_to_history("chat1", "user", "hello")
        agent._add_to_history("chat1", "assistant", "hi")
        assert len(agent._histories["chat1"]) == 2

    def test_history_trim(self) -> None:
        agent = Agent(max_history=2)
        for i in range(10):
            agent._add_to_history("chat1", "user", f"msg {i}")
        # Max history is 2, so *2 = 4 messages kept
        assert len(agent._histories["chat1"]) == 4
