"""Tests for the Agent runtime."""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.agent.runtime import (
    _ECHO_SIMILARITY_THRESHOLD,
    _REPEATED_RESPONSE_FALLBACK,
    Agent,
    _format_response_for_channel,
    _history_text,
    _is_echo,
    _strip_history_note,
)
from machina.connectors.docs.document_store import DocumentChunk
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.citation import AgentResponse, Citation
from machina.domain.failure_mode import FailureMode
from machina.domain.plant import Plant
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import LLMError

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
            equipment_class_code="PU",
        )
    )
    return plant


def _diag_failure_modes() -> list[FailureMode]:
    """Catalog entries mirroring examples/sample_data/cmms/failure_modes.json."""
    return [
        FailureMode(
            code="BEAR-WEAR-01",
            name="Bearing Wear — Drive End",
            category="mechanical",
            typical_indicators=[
                "vibration_velocity_mm_s",
                "bearing_temperature_c",
                "vibration_acceleration_g",
            ],
            recommended_actions=["replace_bearing", "check_alignment", "verify_lubrication"],
        ),
        FailureMode(
            code="SEAL-LEAK-01",
            name="Mechanical Seal Leakage",
            category="mechanical",
            typical_indicators=["seal_pressure_bar", "leakage_rate_ml_min", "flow_rate_m3h"],
            recommended_actions=["replace_seal", "check_shaft_runout", "verify_alignment"],
        ),
        FailureMode(
            code="IMP-EROSION-01",
            name="Impeller Erosion",
            category="mechanical",
            typical_indicators=[
                "vibration_velocity_mm_s",
                "differential_pressure_bar",
                "flow_rate_m3h",
            ],
            recommended_actions=["replace_impeller", "check_fluid_quality", "inspect_casing"],
        ),
        FailureMode(
            code="BELT-WEAR-01",
            name="Conveyor Belt Wear",
            category="mechanical",
            typical_indicators=["belt_speed_m_s", "vibration_velocity_mm_s", "motor_current_a"],
            recommended_actions=["replace_belt", "adjust_tension", "check_rollers"],
        ),
    ]


class _FakeFailureModeConnector:
    """Connector stub declaring ``READ_FAILURE_MODES`` with a catalog."""

    capabilities: ClassVar[list[str]] = ["read_failure_modes"]

    def __init__(self, failure_modes: list[FailureMode] | None = None) -> None:
        self._failure_modes = failure_modes if failure_modes is not None else _diag_failure_modes()

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def read_failure_modes(self) -> list[FailureMode]:
        return list(self._failure_modes)


class _RaisingFailureModeConnector:
    """Stub declaring ``READ_FAILURE_MODES`` whose read always raises.

    Models a registered-but-not-connected provider: the public
    ``read_failure_modes()`` raises ``ConnectorError`` instead of serving
    a catalog.
    """

    capabilities: ClassVar[list[str]] = ["read_failure_modes"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def read_failure_modes(self) -> list[FailureMode]:
        from machina.exceptions import ConnectorError

        raise ConnectorError("not connected")


def _make_diag_plant() -> Plant:
    """Plant with a pump declaring its failure modes and an undeclared asset."""
    plant = Plant(name="Diag Plant")
    plant.register_asset(
        Asset(
            id="P-201",
            name="Cooling Water Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building A",
            criticality=Criticality.A,
            failure_modes=["BEAR-WEAR-01", "SEAL-LEAK-01", "IMP-EROSION-01"],
        )
    )
    plant.register_asset(
        Asset(
            id="HX-101",
            name="Heat Exchanger",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building B",
            criticality=Criticality.B,
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
                equipment_class_code="PU",
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


class _FakeCreateWoConnector(_FakeConnector):
    """Connector that also supports create_work_order."""

    capabilities: ClassVar[list[str]] = [
        "read_assets",
        "read_work_orders",
        "create_work_order",
    ]

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        return work_order


class _FakeGetWoConnector(_FakeConnector):
    """Connector that also supports the get_work_order single fetch."""

    capabilities: ClassVar[list[str]] = [
        "read_assets",
        "read_work_orders",
        "get_work_order",
    ]

    async def get_work_order(self, work_order_id: str) -> WorkOrder | None:
        if work_order_id != "WO-001":
            return None
        return WorkOrder(
            id="WO-001",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="Replace bearing",
        )


class _FakeDocConnector:
    """Connector stub for document search."""

    capabilities: ClassVar[list[str]] = ["search_documents"]

    def __init__(self) -> None:
        self.last_call_kwargs: dict[str, Any] = {}

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def search(self, query: str, **kwargs: Any) -> list[DocumentChunk]:
        self.last_call_kwargs = {"query": query, **kwargs}
        return [
            DocumentChunk(
                content="Pump P-201 bearing replacement procedure",
                source="manual.txt",
                page=1,
                chunk_id="fake-chunk-1",
            )
        ]


class _FakeSparePartsConnector:
    """Connector stub for spare parts."""

    capabilities: ClassVar[list[str]] = ["read_spare_parts"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def read_spare_parts(self, **kwargs: Any) -> list[SparePart]:
        return [
            SparePart(
                sku="SKF-6310",
                name="Deep Groove Ball Bearing",
                manufacturer="SKF",
                compatible_assets=["P-201"],
                stock_quantity=4,
                reorder_point=2,
                lead_time_days=5,
                unit_cost=45.0,
                warehouse_location="W1",
            )
        ]


class _FakeErrorConnector:
    """Connector that errors on read_work_orders."""

    capabilities: ClassVar[list[str]] = ["read_work_orders"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def read_work_orders(self, **kwargs: Any) -> list[WorkOrder]:
        raise RuntimeError("Connection timeout")


class _FakeChannel:
    """Channel stub that delivers one message to the handler and exits.

    Used to exercise :meth:`Agent.run` and the handler lambda in
    ``_run_async()`` without blocking on real stdin or Telegram polling.
    """

    capabilities: ClassVar[list[str]] = ["send_message"]

    def __init__(self, message_text: str = "Tell me about pump P-201") -> None:
        self._message_text = message_text
        self.connected = False
        self.disconnected = False
        self.received_response: str | None = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def listen(self, handler: Any) -> None:
        text = self._message_text

        class _Msg:
            pass

        msg = _Msg()
        msg.text = text  # type: ignore[attr-defined]
        msg.chat_id = "test-chat"  # type: ignore[attr-defined]
        self.received_response = await handler(msg)


class _FakeLLM:
    """Fake LLM provider that returns a canned response."""

    def __init__(self, response: str = "I can help with pump P-201.") -> None:
        self.model = "fake:model"
        self._response = response

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return self._response

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"content": self._response, "tool_calls": None}


class _FakeOpenAIStyleLLM:
    """LLM stub mimicking OpenAI's response shape (``tool_calls=None``)."""

    model = "openai:gpt-4o-mini"

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return "Response from OpenAI-style provider."

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "content": "Response from OpenAI-style provider.",
            "tool_calls": None,
        }


class _FakeOllamaStyleLLM:
    """LLM stub mimicking Ollama's response shape (``tool_calls=[]``).

    Ollama's LiteLLM adapter returns an empty list rather than ``None``
    when the model chose not to call any tool. The agent runtime must
    handle both shapes transparently — this stub covers the ``[]`` case.
    """

    model = "ollama:llama3:8b"

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return "Response from Ollama-style provider."

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "content": "Response from Ollama-style provider.",
            "tool_calls": [],
        }


class _FakeLLMWithToolCalls:
    """Fake LLM that returns a tool call on first invocation, then text."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._call_count = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return "Final answer after tool calls."

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count == 1:
            tc = MagicMock()
            tc.function.name = "search_assets"
            tc.function.arguments = json.dumps({"query": "P-201"})
            tc.id = "call_001"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "The pump P-201 is a cooling water pump.", "tool_calls": None}


class _FakeLLMAlwaysToolCall:
    """Fake LLM that always returns tool calls (for max iteration test)."""

    def __init__(self) -> None:
        self.model = "fake:model"

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return "Exhausted iterations fallback."

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        tc = MagicMock()
        tc.function.name = "search_assets"
        tc.function.arguments = json.dumps({"query": "pump"})
        tc.id = "call_loop"
        return {"content": "", "tool_calls": [tc]}


class _FakeLLMBadArgs:
    """Fake LLM with a tool call whose arguments are invalid JSON."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._call_count = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        return "fallback"

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count == 1:
            tc = MagicMock()
            tc.function.name = "search_assets"
            tc.function.arguments = "{bad json"
            tc.id = "call_bad"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Handled bad args gracefully.", "tool_calls": None}


class _FakeLLMRaises:
    """Fake LLM that raises on any call."""

    def __init__(self) -> None:
        self.model = "fake:model"

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        raise RuntimeError("LLM service unavailable")

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        raise RuntimeError("LLM service unavailable")


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
        assert agent._llm.model == "openai/gpt-4o"

    def test_llm_instance(self) -> None:
        llm = _FakeLLM()
        agent = Agent(llm=llm)  # type: ignore[arg-type]
        assert agent._llm is llm


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
        result = await agent._execute_tool("read_work_orders", {"asset_id": "P-201"})
        assert isinstance(result, list)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_read_work_orders_no_connector(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("read_work_orders", {"asset_id": "P-201"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_work_order_tool(self) -> None:
        conn = _FakeGetWoConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        result = await agent._execute_tool("get_work_order", {"work_order_id": "WO-001"})
        assert result["id"] == "WO-001"
        assert result["asset_id"] == "P-201"

    @pytest.mark.asyncio
    async def test_get_work_order_not_found(self) -> None:
        conn = _FakeGetWoConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        result = await agent._execute_tool("get_work_order", {"work_order_id": "WO-MISSING"})
        assert "error" in result
        assert "WO-MISSING" in result["error"]

    @pytest.mark.asyncio
    async def test_get_work_order_no_connector(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("get_work_order", {"work_order_id": "WO-001"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_create_work_order_tool(self) -> None:
        conn = _FakeCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLM()  # type: ignore[assignment]
        await agent.start()
        result = await agent._execute_tool(
            "create_work_order",
            {
                "asset_id": "P-201",
                "type": "corrective",
                "priority": "high",
                "description": "Replace bearing",
            },
        )
        assert isinstance(result, dict)
        assert result["asset_id"] == "P-201"

    @pytest.mark.asyncio
    async def test_create_work_order_deterministic_id(self) -> None:
        """Identical create requests yield a stable, content-derived ID.

        Regression: the old scheme used ``id(args) % 10000`` which gave a
        different ID on every call, so a model that re-requested the tool
        inside the agent loop produced duplicate work orders with distinct
        IDs (see the quickstart triple-create report). Fresh ``dict``
        instances mimic ``json.loads`` returning a new object per tool call.
        """
        conn = _FakeCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLM()  # type: ignore[assignment]
        await agent.start()
        args = {
            "asset_id": "P-201",
            "type": "corrective",
            "priority": "high",
            "description": "Replace bearing",
        }
        first = await agent._execute_tool("create_work_order", dict(args))
        second = await agent._execute_tool("create_work_order", dict(args))
        assert first["id"].startswith("WO-AUTO-")
        assert first["id"] == second["id"]

    @pytest.mark.asyncio
    async def test_create_work_order_no_connector(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("create_work_order", {"asset_id": "P-201"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_documents_tool(self) -> None:
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        result = await agent._execute_tool("search_documents", {"query": "bearing replacement"})
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "content" in result[0]

    @pytest.mark.asyncio
    async def test_search_documents_no_connector(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("search_documents", {"query": "bearing"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_search_documents_registers_chunks_only_for_current_chat(self) -> None:
        """Tool-call retrieved chunks must NOT leak across concurrent chats.

        Two chats are active at once. A tool call for chat A must only
        populate chat A's _turn_chunks registry, leaving chat B untouched.
        """
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()

        # Simulate two in-flight chats (as handle_message_full would).
        agent._turn_chunks["chat-a"] = {}
        agent._turn_chunks["chat-b"] = {}

        # Tool call attributed to chat A.
        result = await agent._execute_tool(
            "search_documents", {"query": "bearing"}, chat_id="chat-a"
        )
        assert isinstance(result, list)
        assert len(result) >= 1

        # Chat A receives the chunks; chat B stays empty.
        assert agent._turn_chunks["chat-a"], "chat-a should have registered chunks"
        assert agent._turn_chunks["chat-b"] == {}, "chat-b must not see chat-a chunks"

    @pytest.mark.asyncio
    async def test_search_documents_forwards_filters_to_connector(self) -> None:
        """LLM-supplied ``filters`` must reach the connector's search call."""
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        await agent._execute_tool(
            "search_documents",
            {"query": "bearing", "filters": {"doc_type": "procedure"}},
        )
        assert conn.last_call_kwargs.get("filters") == {"doc_type": "procedure"}

    @pytest.mark.asyncio
    async def test_search_documents_ignores_non_dict_filters(self) -> None:
        """Malformed ``filters`` (string, list) must not be passed through."""
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        await agent._execute_tool(
            "search_documents", {"query": "bearing", "filters": "not-a-dict"}
        )
        assert conn.last_call_kwargs.get("filters") is None

    @pytest.mark.asyncio
    async def test_search_documents_default_chat_id_when_not_threaded(self) -> None:
        """Direct callers omitting chat_id register under 'default' only."""
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        agent._turn_chunks["other-chat"] = {}

        await agent._execute_tool("search_documents", {"query": "bearing"})

        assert "default" in agent._turn_chunks
        assert agent._turn_chunks["default"], "default chat should have chunks"
        assert agent._turn_chunks["other-chat"] == {}, "other-chat must stay clean"

    @pytest.mark.asyncio
    async def test_check_spare_parts_tool(self) -> None:
        conn = _FakeSparePartsConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        result = await agent._execute_tool("check_spare_parts", {"asset_id": "P-201"})
        assert isinstance(result, list)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_check_spare_parts_no_connector(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("check_spare_parts", {"asset_id": "P-201"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_diagnose_failure_tool(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        result = await agent._execute_tool(
            "diagnose_failure", {"asset_id": "P-201", "symptoms": ["vibration"]}
        )
        assert result["asset_id"] == "P-201"
        assert "probable_failures" in result
        assert "asset_name" in result

    @pytest.mark.asyncio
    async def test_diagnose_failure_no_asset(self) -> None:
        agent = Agent()
        result = await agent._execute_tool(
            "diagnose_failure", {"asset_id": "NOPE", "symptoms": ["noise"]}
        )
        assert result["asset_id"] == "NOPE"
        assert "note" in result

    @pytest.mark.asyncio
    async def test_get_maintenance_schedule_tool(self) -> None:
        agent = Agent()
        result = await agent._execute_tool("get_maintenance_schedule", {})
        assert "info" in result


class TestDiagnoseFailureCatalog:
    """diagnose_failure matches symptoms against the live failure-mode catalog."""

    @pytest.mark.asyncio
    async def test_token_overlap_matches_canonical_indicators(self) -> None:
        """Free-text 'high vibration' matches modes via the 'vibration' token."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["high vibration"]},
        )
        codes = [f["code"] for f in result["probable_failures"]]
        assert codes == ["BEAR-WEAR-01", "IMP-EROSION-01"]
        top = result["probable_failures"][0]
        # 2 of 3 bearing-wear indicators contain the 'vibration' token.
        assert top["confidence"] == 0.67
        assert top["matching_indicators"] == [
            "vibration_velocity_mm_s",
            "vibration_acceleration_g",
        ]
        assert top["recommended_actions"] == [
            "replace_bearing",
            "check_alignment",
            "verify_lubrication",
        ]
        assert "note" not in result

    @pytest.mark.asyncio
    async def test_exact_indicator_name_still_matches(self) -> None:
        """Fuzzy matching is a superset of exact: canonical names match too."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["bearing_temperature_c"]},
        )
        codes = [f["code"] for f in result["probable_failures"]]
        assert codes == ["BEAR-WEAR-01"]

    @pytest.mark.asyncio
    async def test_declared_asset_excludes_other_assets_modes(self) -> None:
        """The pump never gets the conveyor's belt-wear diagnosis."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["vibration", "belt speed"]},
        )
        codes = {f["code"] for f in result["probable_failures"]}
        assert "BELT-WEAR-01" not in codes
        assert "BEAR-WEAR-01" in codes

    @pytest.mark.asyncio
    async def test_undeclared_asset_falls_back_to_full_catalog_with_note(self) -> None:
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "HX-101", "symptoms": ["high vibration"]},
        )
        codes = {f["code"] for f in result["probable_failures"]}
        assert "BELT-WEAR-01" in codes  # full catalog in play
        assert result["note"] == (
            "Asset declares no failure modes; diagnosis ran against the full failure-mode catalog."
        )

    @pytest.mark.asyncio
    async def test_call_time_harvest_via_declared_capability(self) -> None:
        """The catalog is harvested at call time from capability-declaring providers.

        The stub serves its catalog without requiring a connection, so no
        ``agent.start()`` is needed here; real connectors whose
        ``read_failure_modes()`` raises before connect contribute nothing
        (see the ConnectorError guard tests).
        """
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["seal leakage"]},
        )
        codes = [f["code"] for f in result["probable_failures"]]
        assert codes == ["SEAL-LEAK-01"]

    @pytest.mark.asyncio
    async def test_duplicate_codes_across_connectors_deduped(self) -> None:
        """Two connectors carrying the same catalog yield no duplicate entries."""
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_FakeFailureModeConnector(), _FakeFailureModeConnector()],
        )
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["high vibration"]},
        )
        codes = [f["code"] for f in result["probable_failures"]]
        assert codes == ["BEAR-WEAR-01", "IMP-EROSION-01"]

    @pytest.mark.asyncio
    async def test_results_capped_at_top_five(self) -> None:
        modes = [
            FailureMode(
                code=f"VIB-{i:02d}",
                name=f"Vibration mode {i}",
                category="mechanical",
                typical_indicators=["vibration_velocity_mm_s"] + ["other_indicator"] * i,
            )
            for i in range(7)
        ]
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_FakeFailureModeConnector(failure_modes=modes)],
        )
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "HX-101", "symptoms": ["vibration"]},
        )
        failures = result["probable_failures"]
        assert len(failures) == 5
        confidences = [f["confidence"] for f in failures]
        assert confidences == sorted(confidences, reverse=True)

    @pytest.mark.asyncio
    async def test_unknown_asset_note(self) -> None:
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "GHOST-9", "symptoms": ["vibration"]},
        )
        assert result["probable_failures"] == []
        assert result["note"] == "Asset 'GHOST-9' not found in the asset registry."
        assert "asset_name" not in result

    @pytest.mark.asyncio
    async def test_no_catalog_note(self) -> None:
        agent = Agent(plant=_make_diag_plant())  # no connectors at all
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["vibration"]},
        )
        assert result["probable_failures"] == []
        assert result["note"] == "No failure-mode data configured on any connector."

    @pytest.mark.asyncio
    async def test_no_match_note_lists_known_indicators(self) -> None:
        """A miss tells the model the catalog vocabulary it can re-ask in."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["strange smell"]},
        )
        assert result["probable_failures"] == []
        assert result["note"].startswith("No catalog entry matched these symptoms.")
        # Indicators come from the pump's declared modes only.
        assert "bearing_temperature_c" in result["note"]
        assert "belt_speed_m_s" not in result["note"]

    @pytest.mark.asyncio
    async def test_declared_modes_absent_from_catalog_note(self) -> None:
        """Declared-but-uncatalogued modes get an honest mismatch note.

        Without the early branch, the empty candidate list would render the
        garbled "No catalog entry matched these symptoms. Known indicators: ."
        """
        belt_only = [
            FailureMode(
                code="BELT-WEAR-01",
                name="Conveyor Belt Wear",
                category="mechanical",
                typical_indicators=["belt_speed_m_s"],
            )
        ]
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_FakeFailureModeConnector(failure_modes=belt_only)],
        )
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["vibration"]},
        )
        assert result["probable_failures"] == []
        assert result["note"] == (
            "Asset declares 3 failure mode(s) "
            "(BEAR-WEAR-01, IMP-EROSION-01, SEAL-LEAK-01) but none are present "
            "in the configured catalog (possible configuration mismatch)."
        )
        assert "Known indicators" not in result["note"]

    @pytest.mark.asyncio
    async def test_ranked_by_matched_count_before_ratio(self) -> None:
        """A 1/2-indicator mode must not outrank a 3/7 mode (count first)."""
        modes = [
            FailureMode(
                code="FEW-01",
                name="Few indicators",
                category="mechanical",
                typical_indicators=["vibration_velocity_mm_s", "noise_db"],
            ),
            FailureMode(
                code="MANY-01",
                name="Many indicators",
                category="mechanical",
                typical_indicators=[
                    "vibration_velocity_mm_s",
                    "bearing_temperature_c",
                    "seal_pressure_bar",
                    "flow_rate_m3h",
                    "motor_current_a",
                    "belt_speed_m_s",
                    "oil_debris_ppm",
                ],
            ),
        ]
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_FakeFailureModeConnector(failure_modes=modes)],
        )
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "HX-101", "symptoms": ["vibration", "temperature", "pressure"]},
        )
        failures = result["probable_failures"]
        assert [f["code"] for f in failures] == ["MANY-01", "FEW-01"]
        # MANY-01 wins on matched count (3 vs 1) despite the lower ratio.
        assert len(failures[0]["matching_indicators"]) == 3
        assert len(failures[1]["matching_indicators"]) == 1
        assert failures[0]["confidence"] < failures[1]["confidence"]

    @pytest.mark.asyncio
    async def test_mode_without_indicators_skipped_no_exception(self) -> None:
        """An empty typical_indicators list never divides by zero (skipped)."""
        modes = [
            FailureMode(
                code="NO-IND-01",
                name="No indicators declared",
                category="mechanical",
                typical_indicators=[],
            ),
            FailureMode(
                code="VIB-01",
                name="Vibration mode",
                category="mechanical",
                typical_indicators=["vibration_velocity_mm_s"],
            ),
        ]
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_FakeFailureModeConnector(failure_modes=modes)],
        )
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "HX-101", "symptoms": ["vibration"]},
        )
        codes = [f["code"] for f in result["probable_failures"]]
        assert codes == ["VIB-01"]
        assert "NO-IND-01" not in codes


class TestCollectFailureModes:
    """Capability-gated harvest semantics of ``_collect_failure_modes``."""

    @pytest.mark.asyncio
    async def test_overlapping_codes_first_registration_wins(self) -> None:
        """Two providers sharing a code keep the first-registered entity."""
        first = FailureMode(
            code="BEAR-WEAR-01",
            name="Bearing Wear (first)",
            category="mechanical",
            typical_indicators=["vibration_velocity_mm_s"],
        )
        second = FailureMode(
            code="BEAR-WEAR-01",
            name="Bearing Wear (second)",
            category="mechanical",
            typical_indicators=["bearing_temperature_c"],
        )
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[
                _FakeFailureModeConnector(failure_modes=[first]),
                _FakeFailureModeConnector(failure_modes=[second]),
            ],
        )
        catalog = await agent._collect_failure_modes()
        assert [fm.code for fm in catalog] == ["BEAR-WEAR-01"]
        assert catalog[0].name == "Bearing Wear (first)"

    @pytest.mark.asyncio
    async def test_no_capability_declared_yields_empty_harvest(self) -> None:
        """Connectors without the capability contribute nothing — no probing."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeConnector()])
        assert await agent._collect_failure_modes() == []

    @pytest.mark.asyncio
    async def test_agent_starts_normally_with_no_providers(self) -> None:
        """An agent with zero failure-mode providers starts without error."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeConnector()])
        await agent.start()
        try:
            analyzer = agent._engine._services["failure_analyzer"]
            assert analyzer._failure_modes == []
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_provider_with_empty_catalog_tolerated(self) -> None:
        """A declared provider returning ``[]`` is harmless."""
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_FakeFailureModeConnector(failure_modes=[])],
        )
        assert await agent._collect_failure_modes() == []

    @pytest.mark.asyncio
    async def test_raising_provider_contributes_nothing(self) -> None:
        """A provider raising ConnectorError is skipped, not fatal (R9 guard)."""
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_RaisingFailureModeConnector(), _FakeFailureModeConnector()],
        )
        catalog = await agent._collect_failure_modes()
        # The healthy provider's catalog still arrives.
        assert {fm.code for fm in catalog} == {fm.code for fm in _diag_failure_modes()}

    @pytest.mark.asyncio
    async def test_raising_sole_provider_gives_honest_empty_catalog_note(self) -> None:
        """Diagnosis stays honest when the only provider cannot serve."""
        agent = Agent(
            plant=_make_diag_plant(),
            connectors=[_RaisingFailureModeConnector()],
        )
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": ["vibration"]},
        )
        assert result["probable_failures"] == []
        assert result["note"] == "No failure-mode data configured on any connector."

    @pytest.mark.asyncio
    async def test_workflow_and_diagnose_share_single_source(self) -> None:
        """``_build_domain_services`` and ``diagnose_failure`` read the same catalog."""
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        await agent._build_domain_services()
        analyzer = agent._engine._services["failure_analyzer"]
        analyzer_codes = {fm.code for fm in analyzer._failure_modes}
        harvest_codes = {fm.code for fm in await agent._collect_failure_modes()}
        assert analyzer_codes == harvest_codes == {fm.code for fm in _diag_failure_modes()}


class TestDiagnoseFailureArgCoercion:
    """LLM-boundary args for diagnose_failure degrade at the tool level.

    Hostile shapes (symptoms: null, bare string, mixed-type list, non-string
    asset_id) must yield a tool-level result the model can react to — never
    raise and escalate into a whole-turn LLMError.
    """

    @pytest.mark.asyncio
    async def test_symptoms_none_treated_as_empty(self) -> None:
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": None},
        )
        assert result["symptoms"] == []
        assert result["probable_failures"] == []
        assert "note" in result

    @pytest.mark.asyncio
    async def test_symptoms_bare_string_treated_as_empty(self) -> None:
        # A bare string is NOT iterated character-by-character into tokens.
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": "vibration"},
        )
        assert result["symptoms"] == []
        assert result["probable_failures"] == []

    @pytest.mark.asyncio
    async def test_non_string_list_members_filtered(self) -> None:
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": "P-201", "symptoms": [42, None, "high vibration", {"x": 1}]},
        )
        assert result["symptoms"] == ["high vibration"]
        codes = [f["code"] for f in result["probable_failures"]]
        assert "BEAR-WEAR-01" in codes

    @pytest.mark.asyncio
    async def test_non_string_asset_id_returns_tool_error(self) -> None:
        agent = Agent(plant=_make_diag_plant(), connectors=[_FakeFailureModeConnector()])
        result = await agent._execute_tool(
            "diagnose_failure",
            {"asset_id": 123, "symptoms": ["vibration"]},
        )
        assert result == {"error": "asset_id must be a string"}


class TestDeclineOffSurfaceAction:
    """A plain-prose decline of an unsupported action is a REAL answer.

    The system prompt (capability-honesty constraint, U5) instructs the model
    to decline actions outside the tool/capability surface. The runtime must
    pass that decline through every egress gate untouched: no fallback
    substitution, no suppression — ``is_fallback`` stays ``False`` by
    construction and the text survives intact.
    """

    @pytest.mark.asyncio
    async def test_decline_text_passes_gates_unchanged(self) -> None:
        decline = (
            "I cannot register a new failure mode in the CMMS — none of my "
            "available tools supports that action. I can instead diagnose "
            "probable failure modes from symptoms, search the maintenance "
            "history, or create a corrective work order for P-201."
        )
        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        agent._llm = _FakeLLM(decline)  # type: ignore[assignment]
        await agent.start()

        resp = await agent.handle_message_full(
            "Register a failure mode in the CMMS for pump P-201"
        )

        assert resp.text == decline
        assert resp.is_fallback is False
        assert resp.citations == []


class TestToolResultRecordedOnTrace:
    """The tool-call trace span records the FULL result as parseable JSON.

    The conversational eval's ``expect_tool_result_nonempty`` assertion reads
    ``metadata["result_json"]`` off the traced ``tool_call`` entry — the
    truncated ``output_summary`` (repr-style, 200 chars) is not reliably
    parseable. Recording only; no behavioural change.
    """

    @pytest.mark.asyncio
    async def test_tool_call_span_carries_result_json(self) -> None:
        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        agent._llm = _FakeLLMWithToolCalls()  # type: ignore[assignment]
        await agent.start()

        await agent.handle_message_full("Tell me about P-201")

        tool_entries = [e for e in agent.tracer.entries if e.action == "tool_call"]
        assert tool_entries, "expected at least one traced tool_call"
        entry = tool_entries[0]
        assert entry.operation == "search_assets"
        payload = json.loads(entry.metadata["result_json"])
        # The full structured result round-trips — not a truncated repr.
        assert payload, "recorded result must be non-empty for a successful read"

    @pytest.mark.asyncio
    async def test_result_json_capped_at_64kib(self) -> None:
        """An oversized tool result is truncated with a visible marker.

        A truncated value deliberately fails ``json.loads`` so the eval falls
        back to ``output_summary`` instead of parsing clipped JSON.
        """

        class _HugeWoConnector(_FakeConnector):
            async def read_work_orders(self, **kwargs: Any) -> list[WorkOrder]:
                return [
                    WorkOrder(
                        id="WO-BIG",
                        type=WorkOrderType.CORRECTIVE,
                        priority=Priority.HIGH,
                        asset_id="P-201",
                        description="x" * 80_000,
                    )
                ]

        class _LLMReadWos:
            model = "fake:model"

            def __init__(self) -> None:
                self._calls = 0

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "Done."

            async def complete_with_tools(
                self,
                messages: list[dict[str, str]],
                tools: list[dict[str, Any]],
                **kwargs: Any,
            ) -> dict[str, Any]:
                self._calls += 1
                if self._calls == 1:
                    tc = MagicMock()
                    tc.function.name = "read_work_orders"
                    tc.function.arguments = json.dumps({"asset_id": "P-201"})
                    tc.id = "call_big"
                    return {"content": "", "tool_calls": [tc]}
                return {"content": "Big result handled.", "tool_calls": None}

        agent = Agent(plant=_make_plant(), connectors=[_HugeWoConnector()])
        agent._llm = _LLMReadWos()  # type: ignore[assignment]
        await agent.start()

        await agent.handle_message_full("Show work orders for P-201")

        entry = next(e for e in agent.tracer.entries if e.action == "tool_call")
        recorded = entry.metadata["result_json"]
        assert recorded.endswith("...[truncated]")
        assert len(recorded) == 65_536 + len("...[truncated]")
        with pytest.raises(json.JSONDecodeError):
            json.loads(recorded)


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

    def test_known_tool_names_no_connectors(self) -> None:
        """Leak-disposition surface with ZERO connectors: always-on tools only.

        ``_known_tool_names`` derives from the same ``_get_available_tools``
        source dispatch uses, so without connectors the capability-derived
        tools must NOT be "known" (a leaked ``get_work_order`` then
        dispositions as off-surface and is suppressed, never recovered).
        """
        agent = Agent()
        known = agent._known_tool_names()
        assert "diagnose_failure" in known
        assert "get_maintenance_schedule" in known
        assert "get_work_order" not in known
        assert "create_work_order" not in known

    def test_known_tool_names_with_work_order_read_connector(self) -> None:
        """A GET_WORK_ORDER connector puts get_work_order on the known surface."""
        agent = Agent(connectors=[_FakeGetWoConnector()])
        assert "get_work_order" in agent._known_tool_names()

    def test_with_doc_connector(self) -> None:
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        tools = agent._get_available_tools()
        names = {t["function"]["name"] for t in tools}
        assert "search_documents" in names

    def test_with_spare_parts_connector(self) -> None:
        conn = _FakeSparePartsConnector()
        agent = Agent(connectors=[conn])
        tools = agent._get_available_tools()
        names = {t["function"]["name"] for t in tools}
        assert "check_spare_parts" in names


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


class TestGroundingPersistedToHistory:
    """A turn's cited sources must survive into conversation history.

    Without this, a follow-up like "what are the sources?" has nothing to
    resolve against — the ``<citations>`` block is stripped from the rendered
    text and the per-turn Retrieved Context is never recorded — so the model
    re-runs document search and repeats the whole prior answer.
    """

    def test_finalize_appends_source_note_to_history_only(self) -> None:
        agent = Agent()
        chat_id = "c1"
        agent._turn_chunks[chat_id] = {
            "ck1": {"source": "pump_p201_manual.md", "page": 12, "content": "Heat to 110C"}
        }
        agent._turn_ordered[chat_id] = ["ck1"]
        raw = "Heat the bearings to 110C [1].\n<citations>\n[1]\n</citations>"

        response = agent._finalize_turn(chat_id=chat_id, user_text="procedure?", raw_response=raw)

        # The user-facing text stays clean: no grounding note, no raw block.
        assert "Sources used" not in response.text
        assert "<citations>" not in response.text
        assert response.citations[0].source == "pump_p201_manual.md"

        # History carries the source so a follow-up can answer from memory
        # instead of forcing a fresh document search.
        assistant_entry = agent._histories[chat_id][-1]
        assert assistant_entry["role"] == "assistant"
        assert "pump_p201_manual.md" in assistant_entry["content"]
        assert "Sources used" in assistant_entry["content"]

    def test_finalize_no_note_when_no_citations(self) -> None:
        agent = Agent()
        chat_id = "c2"
        response = agent._finalize_turn(
            chat_id=chat_id, user_text="hi", raw_response="Hello, how can I help?"
        )
        assistant_entry = agent._histories[chat_id][-1]
        assert assistant_entry["content"] == response.text
        assert "Sources used" not in assistant_entry["content"]


class TestHistoryTextHelper:
    """Pure-function tests for the grounding-note builder."""

    @staticmethod
    def _cite(source: str) -> Citation:
        return Citation(chunk_id=source, source=source, page=0)

    def test_no_citations_returns_rendered_unchanged(self) -> None:
        assert _history_text("answer", []) == "answer"

    def test_citations_without_source_add_no_note(self) -> None:
        # A citation with an empty source carries nothing to attribute.
        assert _history_text("answer", [self._cite("")]) == "answer"

    def test_single_source_appended(self) -> None:
        out = _history_text("answer", [self._cite("pump.md")])
        assert out == "answer\n\n[Sources used in this answer: pump.md]"

    def test_multiple_distinct_sources_listed_in_order(self) -> None:
        out = _history_text("answer", [self._cite("a.md"), self._cite("b.md")])
        assert out == "answer\n\n[Sources used in this answer: a.md, b.md]"

    def test_duplicate_sources_deduped_first_seen_order(self) -> None:
        cites = [self._cite("b.md"), self._cite("a.md"), self._cite("b.md")]
        out = _history_text("answer", cites)
        # b.md appears once, in first-seen position, before a.md.
        assert out == "answer\n\n[Sources used in this answer: b.md, a.md]"

    def test_note_written_by_template_is_stripped_for_echo(self) -> None:
        # Couples _history_text's note format to _strip_history_note: a note
        # produced by the template must be fully removed before echo comparison.
        # If the wording drifts in only one place, this round-trip fails.
        body = "answer text"
        with_note = _history_text(body, [self._cite("pump.md")])
        assert with_note != body
        assert _strip_history_note(with_note) == body


# Shared by TestEgressRenormalization and TestIsEchoHelper: a long multi-sentence
# answer whose marker-dense variant exercises marker-stripping in echo comparison.
_SENTENCES = (
    "Replace the drive-end bearing",
    "Grease the bearing housings",
    "Check coupling alignment",
    "Verify vibration levels",
    "Record readings in the CMMS",
    "Close out the work order",
    "Archive the final report",
)


class TestEgressRenormalization:
    """U3 — _finalize_turn renormalizes citation markers at the sole egress.

    Users see ``[1][2]...`` inline and a matching ``[n] source:page`` footer,
    never raw per-turn indices; unresolvable markers are stripped fail-closed;
    the stored history is marker-free on both branches.
    """

    @staticmethod
    def _agent_with_registry(
        chat_id: str,
        chunks: dict[str, tuple[str, int]],
        ordered: list[str],
    ) -> Agent:
        agent = Agent()
        agent._turn_chunks[chat_id] = {
            cid: {"source": src, "page": page, "content": "..."}
            for cid, (src, page) in chunks.items()
        }
        agent._turn_ordered[chat_id] = ordered
        return agent

    def test_high_indices_renormalized_across_placeholder_slots(self) -> None:
        # Multi-call turn: absolute indices [7]/[12] survive empty-chunk_id
        # placeholder slots in _turn_ordered and come out as [1]/[2].
        agent = self._agent_with_registry(
            "c",
            {"ck7": ("bearing_guide.md", 12), "ck12": ("lube.md", 7)},
            ["", "", "", "", "", "", "ck7", "", "", "", "", "ck12"],
        )
        raw = "Replace it [7]. Grease it [12].\n<citations>\n[7]\n[12]\n</citations>"
        resp = agent._finalize_turn(chat_id="c", user_text="q", raw_response=raw)
        assert resp.text == "Replace it [1]. Grease it [2]."
        assert [c.chunk_id for c in resp.citations] == ["ck7", "ck12"]
        channel = _format_response_for_channel(resp)
        assert "[1] bearing_guide.md:12" in channel
        assert "[2] lube.md:7" in channel

    def test_in_range_marker_without_block_entry_stripped(self) -> None:
        # [2] is in range but has no <citations> entry — stripped fail-closed,
        # exactly like out-of-range; the footer keeps a single entry.
        agent = self._agent_with_registry(
            "c", {"ck1": ("a.md", 1), "ck2": ("b.md", 2)}, ["ck1", "ck2"]
        )
        raw = "Cited [1]. Uncited [2].\n<citations>\n[1]\n</citations>"
        resp = agent._finalize_turn(chat_id="c", user_text="q", raw_response=raw)
        assert resp.text == "Cited [1]. Uncited."
        assert len(resp.citations) == 1
        channel = _format_response_for_channel(resp)
        assert channel.count("• [") == 1

    def test_zero_citations_text_byte_identical(self) -> None:
        # No citations parsed: no renumbering, no stripping — bracket-like
        # text reaches the user untouched.
        agent = Agent()
        raw = "See bracket [1] and [9] with no citations block."
        resp = agent._finalize_turn(chat_id="c", user_text="q", raw_response=raw)
        assert resp.text == raw
        assert resp.citations == []

    def test_think_block_marker_does_not_affect_numbering(self) -> None:
        # A [1] inside a scrubbed <think> block never participates in
        # first-appearance numbering.
        agent = self._agent_with_registry("c", {"ck1": ("pump.md", 3)}, ["ck1"])
        raw = (
            "<think>I should cite [1] and maybe [9] here</think>"
            "The answer [1].\n<citations>\n[1]\n</citations>"
        )
        resp = agent._finalize_turn(chat_id="c", user_text="q", raw_response=raw)
        assert resp.text == "The answer [1]."
        assert len(resp.citations) == 1

    def test_history_stored_marker_free_with_note(self) -> None:
        # The user-facing text keeps the renormalized marker; the stored
        # history entry is marker-stripped but keeps the grounding note.
        agent = self._agent_with_registry("c", {"ck1": ("pump.md", 3)}, ["ck1"])
        raw = "Heat to 110C [1].\n<citations>\n[1]\n</citations>"
        resp = agent._finalize_turn(chat_id="c", user_text="q", raw_response=raw)
        assert "[1]" in resp.text
        stored = agent._histories["c"][-1]["content"]
        assert "[1]" not in stored
        assert stored.startswith("Heat to 110C.")
        assert "[Sources used in this answer: pump.md]" in stored

    def test_echo_override_history_is_marker_stripped(self) -> None:
        # The echo path stores the REAL echoed text via history_override,
        # which bypasses _history_text — the marker-stripping must cover that
        # branch too, or the echoed [n] re-enters the next turn's prompt.
        clean = ". ".join(_SENTENCES) + "."
        marked = ". ".join(f"{s} [{i}]" for i, s in enumerate(_SENTENCES, 10)) + "."
        agent = Agent()
        agent._add_to_history("c", "user", "first question")
        agent._add_to_history("c", "assistant", clean)
        resp = agent._finalize_turn(chat_id="c", user_text="second question", raw_response=marked)
        assert resp.is_fallback is True
        assert resp.text == _REPEATED_RESPONSE_FALLBACK
        stored = agent._histories["c"][-1]["content"]
        assert stored == clean  # markers gone, echoed text otherwise intact


class TestRepeatedResponseSuppressed:
    """A turn must not deliver the previous turn's answer verbatim.

    Weak local models routinely copy the prior assistant message straight out
    of conversation history, so the user sees the same long canned paragraph on
    every follow-up. The within-turn dedup guards in ``_llm_loop`` reset each
    turn and cannot catch this — the cross-turn guard in ``_finalize_turn`` does.
    """

    # A realistic, >200-char canned answer of the kind weak models echo.
    _LONG = (
        "I'm a specialized maintenance assistant powered by the Machina "
        "framework. I can help you with equipment information, maintenance "
        "history, procedures and manuals, failure diagnosis, spare parts, work "
        "orders, and maintenance schedules. What would you like to accomplish?"
    )

    def test_first_long_answer_delivered_normally(self) -> None:
        agent = Agent()
        r = agent._finalize_turn(chat_id="c", user_text="q1", raw_response=self._LONG)
        assert r.text == self._LONG
        assert not r.is_fallback

    def test_repeat_on_different_question_is_suppressed(self) -> None:
        agent = Agent()
        agent._finalize_turn(chat_id="c", user_text="chi sei?", raw_response=self._LONG)
        r2 = agent._finalize_turn(
            chat_id="c", user_text="sei sicuro esista questa pompa?", raw_response=self._LONG
        )
        # The user sees an honest, distinct fallback instead of the repeat...
        assert r2.is_fallback
        assert r2.text == _REPEATED_RESPONSE_FALLBACK
        assert r2.text != self._LONG
        # ...but history records the REAL echoed text, so it stays the
        # comparison baseline for the next turn (storing the fallback instead
        # would let the echo leak again on turn 3 — see the three-turn test).
        assert agent._histories["c"][-1]["content"] == self._LONG

    def test_three_consecutive_echoes_all_suppressed(self) -> None:
        # Regression for the alternating-leak bug: if history stored the
        # fallback rather than the real echo, turn 3 would compare against the
        # short fallback, miss, and leak the long answer again.
        agent = Agent()
        agent._finalize_turn(chat_id="c", user_text="q1", raw_response=self._LONG)
        r2 = agent._finalize_turn(chat_id="c", user_text="q2", raw_response=self._LONG)
        r3 = agent._finalize_turn(chat_id="c", user_text="q3", raw_response=self._LONG)
        assert r2.is_fallback and r2.text == _REPEATED_RESPONSE_FALLBACK
        assert r3.is_fallback and r3.text == _REPEATED_RESPONSE_FALLBACK
        assert r3.text != self._LONG

    def test_echo_guard_skipped_on_post_write_narration_path(self) -> None:
        # The two-turn post-write narration passes a write-aware fallback_text.
        # A write has already executed, so the echo guard must NOT replace the
        # narration with the "rephrase / switch model" message (which could
        # imply failure and invite a duplicate write).
        agent = Agent()
        agent._finalize_turn(chat_id="c", user_text="create a WO", raw_response=self._LONG)
        write_fallback = "Done — the create_work_order action completed (WO-123)."
        r2 = agent._finalize_turn(
            chat_id="c",
            user_text="yes",
            raw_response=self._LONG,
            fallback_text=write_fallback,
        )
        # Narration delivered as-is; echo guard did not fire on this path.
        assert not r2.is_fallback
        assert r2.text == self._LONG
        assert r2.text != _REPEATED_RESPONSE_FALLBACK

    def test_suppresses_when_only_assistant_in_history(self) -> None:
        # prev_user is None (e.g. a seeded assistant turn or history truncated
        # mid-pair): the same-question short-circuit is skipped and the content
        # comparison still runs.
        agent = Agent()
        agent._histories["c"] = [{"role": "assistant", "content": self._LONG}]
        r = agent._finalize_turn(chat_id="c", user_text="q", raw_response=self._LONG)
        assert r.is_fallback
        assert r.text == _REPEATED_RESPONSE_FALLBACK

    def test_identical_answer_to_identical_question_is_allowed(self) -> None:
        # Asking the SAME question twice and getting the same answer is
        # legitimate, not a degenerate echo — must NOT be suppressed.
        agent = Agent()
        agent._finalize_turn(
            chat_id="c", user_text="list critical assets", raw_response=self._LONG
        )
        r2 = agent._finalize_turn(
            chat_id="c", user_text="list critical assets", raw_response=self._LONG
        )
        assert not r2.is_fallback
        assert r2.text == self._LONG

    def test_short_identical_answers_not_suppressed(self) -> None:
        # Short generic answers ("Yes.") legitimately recur; the guard only
        # targets long canned paragraphs.
        agent = Agent()
        agent._finalize_turn(chat_id="c", user_text="q1", raw_response="Yes, that is correct.")
        r2 = agent._finalize_turn(
            chat_id="c", user_text="q2", raw_response="Yes, that is correct."
        )
        assert not r2.is_fallback
        assert r2.text == "Yes, that is correct."

    def test_guard_is_per_chat(self) -> None:
        # An echo in one conversation must not suppress the same text as a
        # first answer in a different conversation.
        agent = Agent()
        agent._finalize_turn(chat_id="a", user_text="q1", raw_response=self._LONG)
        r = agent._finalize_turn(chat_id="b", user_text="q1", raw_response=self._LONG)
        assert not r.is_fallback
        assert r.text == self._LONG


class TestIsEchoHelper:
    """Pure-function tests for the cross-turn echo detector."""

    _LONG = "x" * 250

    def test_below_min_length_never_echo(self) -> None:
        assert _is_echo("short", "short") is False

    def test_identical_long_text_is_echo(self) -> None:
        assert _is_echo(self._LONG, self._LONG) is True

    def test_whitespace_and_case_normalized(self) -> None:
        a = "  The  Bearing   Needs Replacement. " + "y" * 220
        b = "the bearing needs replacement. " + "y" * 220
        assert _is_echo(a, b) is True

    def test_history_source_note_ignored_when_comparing(self) -> None:
        body = "Replace the bearing every 2000 hours of operation. " + "z" * 200
        stored = body + "\n\n[Sources used in this answer: pump.md]"
        assert _is_echo(body, stored) is True

    def test_distinct_long_answers_not_echo(self) -> None:
        a = "The cooling pump P-201 is criticality A and located in Building A. " + "a" * 200
        b = "Spare bearing SKF-6205 is out of stock; lead time is six weeks. " + "b" * 200
        assert _is_echo(a, b) is False

    def test_markers_ignored_when_comparing(self) -> None:
        """Answers identical up to inline [n] markers compare as an echo (U3).

        Stored history is marker-stripped, so the comparison must run over the
        marker-stripped representation of BOTH sides — otherwise a marker-dense
        echo of a marker-free stored answer slips the guard. The raw-ratio
        assertion proves this case genuinely needs the stripping (the marker
        noise alone pushes similarity below the threshold).
        """
        import difflib

        clean = ". ".join(_SENTENCES) + "."
        marked = ". ".join(f"{s} [{i}]" for i, s in enumerate(_SENTENCES, 10)) + "."
        assert len(marked) >= 200
        raw_ratio = difflib.SequenceMatcher(
            None, " ".join(marked.split()).lower(), " ".join(clean.split()).lower()
        ).ratio()
        assert raw_ratio < _ECHO_SIMILARITY_THRESHOLD  # would NOT fire without stripping
        assert _is_echo(marked, clean) is True


class _FakeLLMDoubleCreate:
    """Fake LLM that requests create_work_order twice (two loop iterations)
    with identical args, then returns text — mimics a weak model re-issuing a
    write inside the tool-calling loop."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._call_count = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Work order created."

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count <= 2:
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(
                {"asset_id": "P-201", "type": "corrective", "description": "Replace bearing"}
            )
            tc.id = f"call_{self._call_count:03d}"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Work order created.", "tool_calls": None}


class _CountingCreateWoConnector(_FakeCreateWoConnector):
    """Records how many times create_work_order actually executes."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.created_assets: list[str] = []

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        self.create_calls += 1
        self.created_assets.append(work_order.asset_id)
        return work_order


class _FakeLLMTwoDistinctCreates:
    """Requests create_work_order for two DIFFERENT assets, then returns text."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._call_count = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Done."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._call_count += 1
        assets = {1: "P-201", 2: "P-202"}
        if self._call_count in assets:
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(
                {"asset_id": assets[self._call_count], "description": "Replace bearing"}
            )
            tc.id = f"call_{self._call_count:03d}"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Done.", "tool_calls": None}


class TestLoopIdempotency:
    """A re-requested side-effecting tool must not execute twice in one turn."""

    @pytest.mark.asyncio
    async def test_duplicate_create_suppressed_in_loop(self) -> None:
        conn = _CountingCreateWoConnector()
        # confirmations=False isolates the memo behaviour from the HITL gate
        # (U4 gates writes by default; here we exercise the write path itself).
        agent = Agent(connectors=[conn], confirmations=False)
        agent._llm = _FakeLLMDoubleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message("crea un work order per P-201, sostituire cuscinetto")
        # The model asked twice with identical args; the side effect ran once.
        assert conn.create_calls == 1

    @pytest.mark.asyncio
    async def test_distinct_args_not_suppressed(self) -> None:
        """The memo must not over-suppress: distinct args run distinct writes."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], confirmations=False)
        agent._llm = _FakeLLMTwoDistinctCreates()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message("crea due work order")
        assert conn.create_calls == 2

    @pytest.mark.asyncio
    async def test_error_result_not_memoised(self) -> None:
        """A failed side effect must not suppress a legitimate retry in-turn."""
        agent = Agent(confirmations=False)
        agent._llm = _FakeLLMDoubleCreate()  # type: ignore[assignment]
        calls = {"n": 0}

        async def fake_exec(name: str, args: dict[str, Any], *, chat_id: str = "default") -> Any:
            calls["n"] += 1
            return {"error": "transient"} if calls["n"] == 1 else {"id": "WO-OK"}

        agent._execute_tool = fake_exec  # type: ignore[assignment]
        await agent.handle_message("crea un work order per P-201")
        # First call errored (not memoised) → second identical request re-runs.
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_suppressed_duplicate_signals_completion(self) -> None:
        """A suppressed duplicate write must feed back an 'already executed'
        signal so the model stops re-issuing the call instead of looping to
        max_iterations."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], confirmations=False)
        agent._llm = _FakeLLMDoubleCreate()  # type: ignore[assignment]
        await agent.start()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "crea un work order per P-201"}
        ]
        await agent._llm_loop(messages, "chat1")

        from machina.agent.runtime import _DUPLICATE_TOOL_NOTE

        assert conn.create_calls == 1
        tool_payloads = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        suppressed = [
            p for p in tool_payloads if isinstance(p, dict) and p.get("already_executed")
        ]
        assert suppressed, "the suppressed duplicate must carry an already_executed signal"
        # The original result is preserved under "result" for the model to read.
        assert suppressed[0].get("result") is not None
        # The note is the actual loop-breaking signal — pin it so a rename/reword
        # cannot silently defeat the fix.
        assert suppressed[0].get("note") == _DUPLICATE_TOOL_NOTE

    @pytest.mark.asyncio
    async def test_sandbox_duplicate_not_mislabeled(self) -> None:
        """In sandbox the suppressed duplicate must NOT claim a real execution."""
        from machina.agent.runtime import _DUPLICATE_TOOL_NOTE_SANDBOX

        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], sandbox=True)
        agent._llm = _FakeLLMDoubleCreate()  # type: ignore[assignment]
        await agent.start()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "crea un work order per P-201"}
        ]
        await agent._llm_loop(messages, "chat1")

        # Sandbox: the connector write never ran.
        assert conn.create_calls == 0
        tool_payloads = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        suppressed = [p for p in tool_payloads if isinstance(p, dict) and "already_executed" in p]
        assert suppressed, "the suppressed duplicate must still be annotated in sandbox"
        # Must NOT assert a real execution happened, and must use the sandbox note.
        assert suppressed[0]["already_executed"] is False
        assert suppressed[0].get("note") == _DUPLICATE_TOOL_NOTE_SANDBOX

    @pytest.mark.asyncio
    async def test_duplicate_suppression_terminates_early(self) -> None:
        """A model that ignores the signal and re-issues forever must still stop
        well before max_iterations."""
        from machina.agent.runtime import _MAX_DUPLICATE_SUPPRESSIONS

        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], confirmations=False)
        llm = _FakeLLMReissueCreate(reworded=False)
        agent._llm = llm  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message("crea un work order per P-201, sostituire cuscinetto")

        # The side effect ran exactly once, and the loop forced a final answer
        # after the suppression cap rather than running all max_iterations (5).
        assert conn.create_calls == 1
        assert llm._n <= _MAX_DUPLICATE_SUPPRESSIONS + 1


class _FakeLLMRepeatRead:
    """Re-issues the SAME read-only tool call on every iteration and never
    emits a final text answer on its own — mimics a weak local model that
    keeps querying instead of synthesising. The loop must dedup the read and
    force a final answer well before max_iterations."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self.tool_call_invocations = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Final answer."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.tool_call_invocations += 1
        tc = MagicMock()
        tc.function.name = "search_assets"
        tc.function.arguments = json.dumps({"query": "P-201"})
        tc.id = f"call_{self.tool_call_invocations:03d}"
        return {"content": "", "tool_calls": [tc]}


class TestReadOnlyLoopTermination:
    """A read-only tool re-issued verbatim must be served from cache and the
    loop must stop offering tools once an iteration asks for nothing new —
    so a weak model cannot burn every iteration on redundant reads."""

    @pytest.mark.asyncio
    async def test_repeated_read_dedup_and_early_stop(self) -> None:
        from machina.agent.runtime import _DUPLICATE_READ_NOTE

        plant = _make_plant()
        agent = Agent(plant=plant, connectors=[_FakeConnector()])
        llm = _FakeLLMRepeatRead()
        agent._llm = llm  # type: ignore[assignment]

        exec_calls = {"n": 0}
        real_exec = agent._execute_tool

        async def counting_exec(
            name: str, args: dict[str, Any], *, chat_id: str = "default"
        ) -> Any:
            exec_calls["n"] += 1
            return await real_exec(name, args, chat_id=chat_id)

        agent._execute_tool = counting_exec  # type: ignore[assignment]

        messages: list[dict[str, Any]] = [{"role": "user", "content": "tell me about P-201"}]
        result = await agent._llm_loop(messages, "chat1", max_iterations=5)

        # The identical read executed once; every repeat was served from cache.
        assert exec_calls["n"] == 1
        # The loop forced a final answer instead of running all 5 iterations.
        assert result == "Final answer."
        # Deterministic: iter 1 executes + caches, iter 2 is all-repeat (served
        # from cache) → no-progress break before a third tool-offering call.
        assert llm.tool_call_invocations == 2

        # The replayed read carries an explicit "already retrieved" signal.
        tool_payloads = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        replayed = [p for p in tool_payloads if isinstance(p, dict) and p.get("already_retrieved")]
        assert replayed, "the repeated read must be served from cache with a replay note"
        assert replayed[0].get("note") == _DUPLICATE_READ_NOTE

    @pytest.mark.asyncio
    async def test_errored_read_not_cached_and_retried(self) -> None:
        """A read that errors must NOT be cached, and a verbatim retry must
        re-execute the connector (the retry is real work, not a no-op repeat)
        — so a transient failure can recover instead of tripping the
        no-progress break on the retry iteration."""

        class _FakeLLMRetryRead:
            def __init__(self) -> None:
                self.model = "fake:model"
                self._n = 0

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "Recovered."

            async def complete_with_tools(
                self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
            ) -> dict[str, Any]:
                self._n += 1
                if self._n <= 2:
                    tc = MagicMock()
                    tc.function.name = "search_assets"
                    tc.function.arguments = json.dumps({"query": "P-201"})
                    tc.id = f"call_{self._n:03d}"
                    return {"content": "", "tool_calls": [tc]}
                return {"content": "Recovered.", "tool_calls": None}

        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        agent._llm = _FakeLLMRetryRead()  # type: ignore[assignment]

        exec_calls = {"n": 0}

        async def flaky_exec(name: str, args: dict[str, Any], *, chat_id: str = "default") -> Any:
            exec_calls["n"] += 1
            # First attempt errors (must not be cached); retry succeeds.
            return {"error": "transient"} if exec_calls["n"] == 1 else [{"id": "P-201"}]

        agent._execute_tool = flaky_exec  # type: ignore[assignment]

        messages: list[dict[str, Any]] = [{"role": "user", "content": "about P-201"}]
        result = await agent._llm_loop(messages, "chat1", max_iterations=5)

        # The errored read was re-executed (not served from cache) on the retry.
        assert exec_calls["n"] == 2
        assert result == "Recovered."

    @pytest.mark.asyncio
    async def test_mixed_new_and_repeat_does_not_break_early(self) -> None:
        """An iteration that pairs a genuinely-new read with a repeated one
        makes progress, so the no-progress break must NOT fire — the loop keeps
        running while the model still asks for new information."""

        class _FakeLLMMixed:
            def __init__(self) -> None:
                self.model = "fake:model"
                self._n = 0

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "Done."

            async def complete_with_tools(
                self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
            ) -> dict[str, Any]:
                self._n += 1
                if self._n <= 3:
                    repeat = MagicMock()
                    repeat.function.name = "search_assets"
                    repeat.function.arguments = json.dumps({"query": "P-201"})
                    repeat.id = f"rep_{self._n:03d}"
                    fresh = MagicMock()
                    fresh.function.name = "search_assets"
                    fresh.function.arguments = json.dumps({"query": f"asset-{self._n}"})
                    fresh.id = f"new_{self._n:03d}"
                    return {"content": "", "tool_calls": [repeat, fresh]}
                return {"content": "Done.", "tool_calls": None}

        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        llm = _FakeLLMMixed()
        agent._llm = llm  # type: ignore[assignment]

        messages: list[dict[str, Any]] = [{"role": "user", "content": "explore"}]
        result = await agent._llm_loop(messages, "chat1", max_iterations=5)

        # The fresh call each iteration kept progress alive: the model reached
        # its own no-tool-call terminus (iteration 4) rather than being cut off.
        assert result == "Done."
        assert llm._n == 4

    @pytest.mark.asyncio
    async def test_distinct_reads_have_independent_cache_slots(self) -> None:
        """Two different read tools called once each must both execute — the
        cache key is per (name + args), so one read must not replay from the
        other's slot."""

        class _FakeLLMTwoReads:
            def __init__(self) -> None:
                self.model = "fake:model"
                self._n = 0

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "Done."

            async def complete_with_tools(
                self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
            ) -> dict[str, Any]:
                self._n += 1
                calls = {
                    1: ("search_assets", {"query": "P-201"}),
                    2: ("read_work_orders", {"asset_id": "P-201"}),
                }
                if self._n in calls:
                    name, args = calls[self._n]
                    tc = MagicMock()
                    tc.function.name = name
                    tc.function.arguments = json.dumps(args)
                    tc.id = f"call_{self._n:03d}"
                    return {"content": "", "tool_calls": [tc]}
                return {"content": "Done.", "tool_calls": None}

        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        agent._llm = _FakeLLMTwoReads()  # type: ignore[assignment]

        exec_calls: list[str] = []

        async def recording_exec(
            name: str, args: dict[str, Any], *, chat_id: str = "default"
        ) -> Any:
            exec_calls.append(name)
            return [{"id": "X"}]

        agent._execute_tool = recording_exec  # type: ignore[assignment]

        messages: list[dict[str, Any]] = [{"role": "user", "content": "about P-201"}]
        await agent._llm_loop(messages, "chat1", max_iterations=5)

        # Both distinct reads executed; neither was a false cache hit.
        assert exec_calls == ["search_assets", "read_work_orders"]


class TestMutatingToolsRegistry:
    """The per-turn memo guard is sourced from the canonical tool registry."""

    def test_mutating_tools_are_real_builtins(self) -> None:
        from machina.llm.tools import BUILTIN_TOOLS, MUTATING_TOOLS

        names = {t["function"]["name"] for t in BUILTIN_TOOLS}
        assert names >= MUTATING_TOOLS, "MUTATING_TOOLS names must exist in BUILTIN_TOOLS"

    def test_known_write_tools_classified(self) -> None:
        from machina.agent.runtime import _SIDE_EFFECTING_TOOLS

        assert "create_work_order" in _SIDE_EFFECTING_TOOLS
        assert "execute_workflow" in _SIDE_EFFECTING_TOOLS


class TestHandleMessage:
    """Test handle_message — full pipeline."""

    @pytest.mark.asyncio
    async def test_simple_message(self) -> None:
        agent = Agent()
        agent._llm = _FakeLLM("Hello! I can help.")  # type: ignore[assignment]
        response = await agent.handle_message("Hello")
        assert response == "Hello! I can help."

    @pytest.mark.asyncio
    async def test_message_with_entity_resolution(self) -> None:
        plant = _make_plant()
        conn = _FakeConnector()
        agent = Agent(plant=plant, connectors=[conn])
        agent._llm = _FakeLLM("P-201 is a cooling water pump.")  # type: ignore[assignment]
        await agent.start()
        response = await agent.handle_message("Tell me about P-201")
        assert "P-201" in response

    @pytest.mark.asyncio
    async def test_message_updates_history(self) -> None:
        agent = Agent()
        agent._llm = _FakeLLM("response text")  # type: ignore[assignment]
        await agent.handle_message("user input", chat_id="test_chat")
        history = agent._histories["test_chat"]
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "user input"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "response text"

    @pytest.mark.asyncio
    async def test_message_llm_error_raises(self) -> None:
        agent = Agent()
        agent._llm = _FakeLLMRaises()  # type: ignore[assignment]
        with pytest.raises(LLMError, match="LLM call failed"):
            await agent.handle_message("test")


class TestHandleMessageFullCitations:
    """End-to-end: fake LLM emits citations → AgentResponse populated."""

    @pytest.mark.asyncio
    async def test_full_chain_populates_citations(self) -> None:
        """LLM output with valid citations block → citations on AgentResponse."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()  # Returns chunk with chunk_id='fake-chunk-1'.
        agent = Agent(plant=plant, connectors=[doc_conn])

        llm_response = (
            "Replace the bearing every 2000 hours [manual.txt:1].\n\n"
            "<citations>\n"
            "fake-chunk-1 | manual.txt | 1\n"
            "</citations>"
        )
        agent._llm = _FakeLLM(llm_response)  # type: ignore[assignment]
        await agent.start()

        response = await agent.handle_message_full("Tell me about P-201 bearing")

        # Citations populated from the per-turn registry.
        assert len(response.citations) == 1
        assert response.citations[0].chunk_id == "fake-chunk-1"
        assert response.citations[0].source == "manual.txt"
        assert response.citations[0].page == 1
        # Inline marker preserved, citations block stripped.
        assert "[manual.txt:1]" in response.text
        assert "<citations>" not in response.text

    @pytest.mark.asyncio
    async def test_handle_message_backcompat_strips_block(self) -> None:
        """handle_message (str API) returns rendered text with block stripped."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[doc_conn])

        llm_response = (
            "Procedure [manual.txt:1].\n\n<citations>\nfake-chunk-1 | manual.txt | 1\n</citations>"
        )
        agent._llm = _FakeLLM(llm_response)  # type: ignore[assignment]
        await agent.start()

        text = await agent.handle_message("P-201 bearing")
        assert isinstance(text, str)
        assert "<citations>" not in text
        assert "[manual.txt:1]" in text

    @pytest.mark.asyncio
    async def test_turn_chunks_cleared_after_handle(self) -> None:
        """The per-chat registry slot must not persist across turns."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[doc_conn])
        agent._llm = _FakeLLM("Hello.")  # type: ignore[assignment]
        await agent.start()

        await agent.handle_message_full("test", chat_id="chat-x")
        assert "chat-x" not in agent._turn_chunks

    @pytest.mark.asyncio
    async def test_unknown_chunk_id_dropped(self) -> None:
        """LLM citing a chunk_id that wasn't retrieved → silently dropped."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[doc_conn])

        llm_response = (
            "Body.\n<citations>\n"
            "fake-chunk-1 | manual.txt | 1\n"
            "ghost-chunk | fake-source | 99\n"
            "</citations>"
        )
        agent._llm = _FakeLLM(llm_response)  # type: ignore[assignment]
        await agent.start()

        response = await agent.handle_message_full("P-201")
        chunk_ids = {c.chunk_id for c in response.citations}
        assert chunk_ids == {"fake-chunk-1"}  # ghost-chunk dropped


class TestIndexCitationContract:
    """Index-based citation contract end-to-end (U1)."""

    @pytest.mark.asyncio
    async def test_index_citation_resolves_end_to_end(self) -> None:
        """AE5: the model cites by visible ``[1]`` and it resolves."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()  # one chunk, chunk_id='fake-chunk-1'
        agent = Agent(plant=plant, connectors=[doc_conn])

        # The model never sees the chunk_id — it cites by index only.
        llm_response = "Replace the bearing [1].\n\n<citations>\n[1]\n</citations>"
        agent._llm = _FakeLLM(llm_response)  # type: ignore[assignment]
        await agent.start()

        response = await agent.handle_message_full("Tell me about P-201 bearing")
        assert len(response.citations) == 1
        assert response.citations[0].chunk_id == "fake-chunk-1"
        assert response.citations[0].source == "manual.txt"
        assert "[1]" in response.text
        assert "<citations>" not in response.text

    @pytest.mark.asyncio
    async def test_bare_source_fallback_end_to_end(self) -> None:
        """AE6: the model cites a bare filename → fallback resolves it."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[doc_conn])

        llm_response = "Procedure [manual.txt].\n\n<citations>\nmanual.txt\n</citations>"
        agent._llm = _FakeLLM(llm_response)  # type: ignore[assignment]
        await agent.start()

        response = await agent.handle_message_full("P-201 bearing")
        assert len(response.citations) == 1
        assert response.citations[0].chunk_id == "fake-chunk-1"

    @pytest.mark.asyncio
    async def test_out_of_range_index_dropped_end_to_end(self) -> None:
        """Regression guard: an out-of-range index with no fallback drops."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[doc_conn])

        llm_response = "Body.\n\n<citations>\n[9]\n</citations>"
        agent._llm = _FakeLLM(llm_response)  # type: ignore[assignment]
        await agent.start()

        response = await agent.handle_message_full("P-201")
        assert response.citations == []


class TestRegisterDocumentResults:
    """Ordered index map vs. filtered chunk registry (U1 off-by-k guard)."""

    def test_ordered_map_includes_empty_chunk_id_slots(self) -> None:
        """The display index must not drift when an earlier chunk_id is empty.

        ``_register_document_results`` skips empty chunk_ids in the registry
        (the fallback map) but MUST keep them as positional placeholders in
        the ordered map, so the visible ``[2]`` resolves to the second
        *displayed* chunk, not the first *registered* one.
        """
        agent = Agent()
        results = [
            {"chunk_id": "", "source": "a.md", "page": 0, "content": "x"},
            {"chunk_id": "real-2", "source": "b.md", "page": 0, "content": "y"},
        ]
        agent._register_document_results("chat-z", results)

        ordered = agent._turn_ordered["chat-z"]
        assert ordered == ["", "real-2"]
        # [2] (display position) resolves to real-2.
        assert ordered[1] == "real-2"
        # The filtered registry skipped the empty-chunk_id row.
        assert "real-2" in agent._turn_chunks["chat-z"]
        assert "" not in agent._turn_chunks["chat-z"]

    def test_ordered_map_truncates_to_five(self) -> None:
        """Mirrors format_document_results' ``[:5]`` display cap."""
        agent = Agent()
        results = [
            {"chunk_id": f"c{i}", "source": f"{i}.md", "page": 0, "content": "x"} for i in range(8)
        ]
        agent._register_document_results("chat-z", results)
        assert len(agent._turn_ordered["chat-z"]) == 5
        assert agent._turn_ordered["chat-z"] == ["c0", "c1", "c2", "c3", "c4"]


class TestSearchDocumentsToolIndex:
    """The search_documents tool result carries a visible citation index."""

    @pytest.mark.asyncio
    async def test_tool_result_has_citation_index(self) -> None:
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        agent._turn_chunks["chat-a"] = {}
        agent._turn_ordered["chat-a"] = []

        result = await agent._execute_tool(
            "search_documents", {"query": "bearing"}, chat_id="chat-a"
        )
        assert isinstance(result, list)
        assert result[0]["citation_index"] == 1
        # The ordered map registers the tool-retrieved chunk by display pos.
        assert agent._turn_ordered["chat-a"] == ["fake-chunk-1"]

    @pytest.mark.asyncio
    async def test_tool_index_offset_after_prefetch(self) -> None:
        """Tool indices continue after pre-fetch chunks already displayed."""
        conn = _FakeDocConnector()
        agent = Agent(connectors=[conn])
        await agent.start()
        # Simulate one pre-fetch chunk already displayed this turn as [1].
        agent._turn_chunks["chat-a"] = {}
        agent._turn_ordered["chat-a"] = ["prefetch-1"]

        result = await agent._execute_tool(
            "search_documents", {"query": "bearing"}, chat_id="chat-a"
        )
        # The tool chunk is displayed as [2], not [1].
        assert result[0]["citation_index"] == 2
        assert agent._turn_ordered["chat-a"] == ["prefetch-1", "fake-chunk-1"]

    @pytest.mark.asyncio
    async def test_tool_path_index_citation_resolves_end_to_end(self) -> None:
        """A ``[n]`` citation resolves for a chunk retrieved via the tool."""
        plant = _make_plant()
        doc_conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[doc_conn])

        # Two-step fake LLM: first turn calls search_documents, then cites [1].
        class _ToolThenCiteLLM:
            def __init__(self) -> None:
                self.model = "fake:model"
                self._calls = 0

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "Replace the bearing [1].\n\n<citations>\n[1]\n</citations>"

            async def complete_with_tools(
                self,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]],
                **kwargs: Any,
            ) -> dict[str, Any]:
                self._calls += 1
                if self._calls == 1:
                    tc = MagicMock()
                    tc.function.name = "search_documents"
                    tc.function.arguments = json.dumps({"query": "bearing"})
                    tc.id = "tc1"
                    return {"content": "", "tool_calls": [tc]}
                return {
                    "content": "Replace the bearing [1].\n\n<citations>\n[1]\n</citations>",
                    "tool_calls": None,
                }

        agent._llm = _ToolThenCiteLLM()  # type: ignore[assignment]
        await agent.start()

        response = await agent.handle_message_full("How do I replace the P-201 bearing?")
        assert len(response.citations) == 1
        assert response.citations[0].chunk_id == "fake-chunk-1"


class TestGatherContext:
    """Test _gather_context — retrieves data from connectors."""

    @pytest.mark.asyncio
    async def test_no_entities(self) -> None:
        agent = Agent()
        context = await agent._gather_context("hello", [])
        assert context == {"resolved_entities": []}

    @pytest.mark.asyncio
    async def test_with_work_orders(self) -> None:
        plant = _make_plant()
        conn = _FakeConnector()
        agent = Agent(plant=plant, connectors=[conn])
        await agent.start()
        resolved = agent._resolver.resolve("P-201")
        assert len(resolved) > 0
        context = await agent._gather_context("P-201 status", resolved)
        assert "work_orders" in context
        assert len(context["work_orders"]) >= 1

    @pytest.mark.asyncio
    async def test_with_spare_parts(self) -> None:
        plant = _make_plant()
        conn = _FakeSparePartsConnector()
        agent = Agent(plant=plant, connectors=[conn])
        await agent.start()
        resolved = agent._resolver.resolve("P-201")
        context = await agent._gather_context("spare parts for P-201", resolved)
        assert "spare_parts" in context
        assert len(context["spare_parts"]) >= 1

    @pytest.mark.asyncio
    async def test_with_documents(self) -> None:
        plant = _make_plant()
        conn = _FakeDocConnector()
        agent = Agent(plant=plant, connectors=[conn])
        await agent.start()
        resolved = agent._resolver.resolve("P-201")
        context = await agent._gather_context("bearing procedure", resolved)
        assert "document_results" in context
        assert len(context["document_results"]) >= 1
        assert "content" in context["document_results"][0]

    @pytest.mark.asyncio
    async def test_error_handling(self) -> None:
        """Connector error should not crash context gathering."""
        plant = _make_plant()
        conn = _FakeErrorConnector()
        agent = Agent(plant=plant, connectors=[conn])
        await agent.start()
        resolved = agent._resolver.resolve("P-201")
        # Should not raise despite connector error
        context = await agent._gather_context("P-201", resolved)
        # work_orders should NOT be in context since the connector errored
        assert "work_orders" not in context


class TestBuildMessages:
    """Test _build_messages — assembles LLM message list."""

    def test_basic(self) -> None:
        agent = Agent()
        messages = agent._build_messages("hello", "chat1", {"resolved_entities": []})
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "hello"

    def test_with_history(self) -> None:
        agent = Agent()
        agent._add_to_history("chat1", "user", "previous question")
        agent._add_to_history("chat1", "assistant", "previous answer")
        messages = agent._build_messages("new question", "chat1", {"resolved_entities": []})
        # system + history(2) + user = 4
        assert len(messages) == 4
        assert messages[1]["content"] == "previous question"

    def test_with_context_data(self) -> None:
        plant = _make_plant()
        agent = Agent(plant=plant)
        resolved = agent._resolver.resolve("P-201")
        context_data = {
            "resolved_entities": resolved,
            "asset": plant.get_asset("P-201"),
            "work_orders": [
                WorkOrder(
                    id="WO-001",
                    type=WorkOrderType.CORRECTIVE,
                    priority=Priority.HIGH,
                    asset_id="P-201",
                    description="Replace bearing",
                )
            ],
        }
        messages = agent._build_messages("P-201 status", "chat1", context_data)
        # Should have system + context system + user = 3
        assert len(messages) >= 3
        # The context message should be the second system message
        context_msgs = [m for m in messages if "Retrieved Context" in m.get("content", "")]
        assert len(context_msgs) == 1


class TestLlmLoop:
    """Test _llm_loop — tool-calling orchestration."""

    @pytest.mark.asyncio
    async def test_no_tools(self) -> None:
        """Agent with no connectors → no tools → calls complete() directly."""
        agent = Agent()
        agent._llm = _FakeLLM("Direct response")  # type: ignore[assignment]
        messages = [{"role": "user", "content": "hello"}]
        result = await agent._llm_loop(messages, "chat1")
        assert result == "Direct response"

    @pytest.mark.asyncio
    async def test_with_tool_call(self) -> None:
        """LLM returns a tool call, then a text response."""
        plant = _make_plant()
        agent = Agent(plant=plant, connectors=[_FakeConnector()])
        agent._llm = _FakeLLMWithToolCalls()  # type: ignore[assignment]
        messages = [{"role": "user", "content": "Tell me about P-201"}]
        result = await agent._llm_loop(messages, "chat1")
        assert "P-201" in result

    @pytest.mark.asyncio
    async def test_max_iterations(self) -> None:
        """LLM always returns tool calls → hits max iterations → falls back."""
        plant = _make_plant()
        agent = Agent(plant=plant, connectors=[_FakeConnector()])
        agent._llm = _FakeLLMAlwaysToolCall()  # type: ignore[assignment]
        messages = [{"role": "user", "content": "loop test"}]
        result = await agent._llm_loop(messages, "chat1", max_iterations=3)
        assert result == "Exhausted iterations fallback."

    @pytest.mark.asyncio
    async def test_bad_json_args(self) -> None:
        """Tool call with invalid JSON args should be handled gracefully."""
        plant = _make_plant()
        agent = Agent(plant=plant, connectors=[_FakeConnector()])
        agent._llm = _FakeLLMBadArgs()  # type: ignore[assignment]
        messages = [{"role": "user", "content": "test"}]
        result = await agent._llm_loop(messages, "chat1")
        assert "bad args" in result.lower() or isinstance(result, str)

    @pytest.mark.asyncio
    async def test_tool_call_returns_no_content(self) -> None:
        """When tool_calls is None and content is empty, return empty string."""
        agent = Agent()
        fake_llm = _FakeLLM()

        # Override to return empty content, no tool calls
        async def _empty_response(messages: Any, tools: Any, **kw: Any) -> dict[str, Any]:
            return {"content": "", "tool_calls": None}

        fake_llm.complete_with_tools = _empty_response  # type: ignore[assignment]
        agent._llm = fake_llm  # type: ignore[assignment]
        agent._registry.register("fake", _FakeConnector())
        messages = [{"role": "user", "content": "test"}]
        result = await agent._llm_loop(messages, "chat1")
        assert result == ""


class TestEmptyResponseFallback:
    """An empty rendered answer must surface a fallback, never blank output.

    ``_llm_loop`` faithfully returns the model's empty string (asserted in
    ``test_tool_call_returns_no_content``); the user-facing substitution
    happens one layer up, in ``_finalize_turn``.
    """

    def test_empty_raw_response_yields_fallback(self) -> None:
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        agent = Agent()
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response="")
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.citations == []
        # The fallback is flagged so callers can tell it apart from a real answer.
        assert resp.is_fallback is True

    def test_citations_only_response_yields_fallback(self) -> None:
        """A response that is nothing but a citations block strips to empty."""
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        agent = Agent()
        raw = "<citations>\n[1]\n</citations>"
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response=raw)
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.citations == []
        assert resp.is_fallback is True

    def test_whitespace_only_response_yields_fallback(self) -> None:
        """The guard is strip()-based, so whitespace-only output also falls back."""
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        agent = Agent()
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response="  \n  ")
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.citations == []
        assert resp.is_fallback is True

    def test_custom_fallback_text_is_used(self) -> None:
        """The narration path can supply a write-aware fallback string."""
        agent = Agent()
        resp = agent._finalize_turn(
            chat_id="c", user_text="hi", raw_response="", fallback_text="Done — created WO-1."
        )
        assert resp.text == "Done — created WO-1."
        assert resp.is_fallback is True

    def test_nonempty_response_is_untouched(self) -> None:
        """A real answer must pass through unchanged — no false-positive fallback."""
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        agent = Agent()
        resp = agent._finalize_turn(
            chat_id="c", user_text="hi", raw_response="Replace the bearing."
        )
        assert resp.text == "Replace the bearing."
        assert resp.text != _EMPTY_RESPONSE_FALLBACK
        assert resp.is_fallback is False


class TestDegenerateJsonAnswerGuard:
    """A rendered answer that is an EMPTY JSON container must fall back.

    2026-06-10 post-fix deepseek-r1:8b eval: 7 turns answered literally
    ``{}``, typically right after a leaked-read recovery. The text carries
    zero information — ``_finalize_turn`` treats it exactly like an empty
    completion (empty-response fallback + ``is_fallback``). Corpus twin:
    ``degenerate-empty-json-answer.json``.
    """

    @pytest.mark.parametrize("raw", ["{}", "[]", "  {}  ", "{ }", "[\n]"])
    def test_empty_json_container_yields_empty_fallback(self, raw: str) -> None:
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        agent = Agent()
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response=raw)
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.citations == []
        assert resp.is_fallback is True

    def test_nonempty_json_answer_passes_through(self) -> None:
        """A legitimate (non-tool-shaped) JSON data answer is never suppressed."""
        agent = Agent()
        raw = '{"status": "ok", "open_work_orders": 3}'
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response=raw)
        assert resp.text == raw
        assert resp.is_fallback is False

    def test_non_container_json_is_out_of_scope(self) -> None:
        """``null`` (and other non-container JSON) passes through by design."""
        agent = Agent()
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response="null")
        assert resp.text == "null"
        assert resp.is_fallback is False

    def test_think_block_wrapping_empty_object_is_caught(self) -> None:
        """The guard runs on the SCRUBBED text: reasoning + `{}` still falls back."""
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        agent = Agent()
        raw = "<think>I should answer now.</think>{}"
        resp = agent._finalize_turn(chat_id="c", user_text="hi", raw_response=raw)
        assert resp.text == _EMPTY_RESPONSE_FALLBACK
        assert resp.is_fallback is True

    def test_custom_fallback_text_is_used(self) -> None:
        """The post-write narration path keeps its write-aware fallback string."""
        agent = Agent()
        resp = agent._finalize_turn(
            chat_id="c", user_text="hi", raw_response="{}", fallback_text="Done — created WO-1."
        )
        assert resp.text == "Done — created WO-1."
        assert resp.is_fallback is True


class TestFormatResponseForChannel:
    """Channel handler must surface citations alongside the rendered text."""

    def test_no_citations_passthrough(self) -> None:
        response = AgentResponse(text="Just an answer.")
        assert _format_response_for_channel(response) == "Just an answer."

    def test_citations_appended_as_numbered_sources_footer(self) -> None:
        # The footer enumerates the (reordered) citations list 1-based, so
        # entry numbers line up with the renormalized inline [n] markers.
        response = AgentResponse(
            text="Replace bearing every 2000h [1], re-grease at half interval [2].",
            citations=[
                Citation(chunk_id="c1", source="manuals/pump.pdf", page=42),
                Citation(chunk_id="c2", source="manuals/pump.pdf", page=43),
            ],
        )
        out = _format_response_for_channel(response)
        assert "— Sources:" in out
        assert "[1] manuals/pump.pdf:42" in out
        assert "[2] manuals/pump.pdf:43" in out

    def test_footer_entry_without_page_omits_page_suffix(self) -> None:
        response = AgentResponse(
            text="See the manual [1].",
            citations=[Citation(chunk_id="c1", source="manuals/pump.pdf", page=0)],
        )
        out = _format_response_for_channel(response)
        assert "[1] manuals/pump.pdf" in out
        assert "manuals/pump.pdf:" not in out

    def test_footer_entry_falls_back_to_chunk_id(self) -> None:
        response = AgentResponse(
            text="Grounded answer.",
            citations=[Citation(chunk_id="abc123", source="", page=4)],
        )
        out = _format_response_for_channel(response)
        assert "[1] abc123" in out


class TestRunAsync:
    """Test run() / _run_async() — agent lifecycle with channels."""

    @pytest.mark.asyncio
    async def test_no_channels_returns_early(self) -> None:
        agent = Agent(channels=[])
        agent._llm = _FakeLLM()  # type: ignore[assignment]
        # Should return without blocking
        await agent._run_async()

    @pytest.mark.asyncio
    async def test_with_channel(self) -> None:
        """Verify channel listen is called and agent stops on KeyboardInterrupt."""
        channel = AsyncMock()
        channel.capabilities = ["send_message", "receive_message"]
        channel.listen = AsyncMock(side_effect=KeyboardInterrupt)

        agent = Agent(channels=[channel])
        agent._llm = _FakeLLM()  # type: ignore[assignment]
        await agent._run_async()

        channel.connect.assert_awaited_once()
        channel.listen.assert_awaited_once()
        channel.disconnect.assert_awaited_once()


class TestAgentRun:
    """Tests for the synchronous :meth:`Agent.run` entry point."""

    def test_run_drives_start_listen_stop(self) -> None:
        """Covers runtime.py line 210 (asyncio.run wrapper) and 223-224
        (the _handler lambda being invoked by a channel)."""
        plant = _make_plant()
        channel = _FakeChannel("What's the status of P-201?")
        agent = Agent(
            plant=plant,
            connectors=[_FakeConnector()],
            channels=[channel],
        )
        agent._llm = _FakeLLM("P-201 is running normally.")  # type: ignore[assignment]

        # Blocking sync call — exits when _FakeChannel.listen returns
        agent.run()

        # Channel lifecycle fully exercised: connect → listen → disconnect
        assert channel.connected is True
        assert channel.disconnected is True
        # The _handler lambda was invoked with our message and returned
        # the stub LLM's canned response
        assert channel.received_response == "P-201 is running normally."
        # start() loaded assets from _FakeConnector.read_assets()
        assert "P-201" in plant.assets


class TestLLMProviderSwap:
    """MACHINA_SPEC R7 AC: same agent produces identical behaviour across
    different LLM provider implementations."""

    @pytest.mark.parametrize(
        ("llm_factory", "expected_prefix"),
        [
            (_FakeOpenAIStyleLLM, "Response from OpenAI-style"),
            (_FakeOllamaStyleLLM, "Response from Ollama-style"),
        ],
        ids=["openai-style", "ollama-style"],
    )
    @pytest.mark.asyncio
    async def test_same_scenario_with_different_providers(
        self,
        llm_factory: Any,
        expected_prefix: str,
    ) -> None:
        """Run the identical agent setup and message through two different
        provider stubs (one returning ``tool_calls=None``, the other
        ``tool_calls=[]``). The runtime must handle both shapes and the
        response must propagate through in both cases."""
        agent = Agent(
            plant=_make_plant(),
            connectors=[_FakeConnector()],
            llm=llm_factory(),
        )
        await agent.start()
        try:
            response = await agent.handle_message(
                "What's the status of P-201?",
                chat_id="t",
            )
            assert response.startswith(expected_prefix)
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_no_tools_falls_back_to_complete(self) -> None:
        """When ``_get_available_tools`` returns an empty list, ``_llm_loop``
        must take the ``complete()`` fallback path instead of
        ``complete_with_tools``.

        In normal operation ``_get_available_tools`` always includes two
        built-in tools (``diagnose_failure`` and ``get_maintenance_schedule``),
        so this branch is only reachable by overriding the method. We do that
        here to prove the fallback is wired up correctly for any future
        deployment that strips built-in tools.

        Covers runtime.py lines 398-399 (previously uncovered).
        """
        agent = Agent(plant=_make_plant(), llm=_FakeOpenAIStyleLLM())
        # Force empty tool list to hit the complete() branch
        agent._get_available_tools = lambda: []  # type: ignore[method-assign]
        await agent.start()
        try:
            response = await agent.handle_message("Hi", chat_id="t")
            assert "OpenAI-style" in response
        finally:
            await agent.stop()


# ---------------------------------------------------------------------------
# Workflow integration tests
# ---------------------------------------------------------------------------


class TestAgentWorkflows:
    """Test Agent workflow registration and triggering."""

    def test_init_with_workflows(self) -> None:
        from machina.workflows import Step, Workflow

        wf = Workflow(name="Test WF", steps=[Step("s1")])
        agent = Agent(workflows=[wf])
        assert "Test WF" in agent.workflows
        assert agent.workflows["Test WF"] is wf

    def test_register_workflow(self) -> None:
        from machina.workflows import Step, Workflow

        agent = Agent()
        wf = Workflow(name="Dynamic WF", steps=[Step("s1")])
        agent.register_workflow(wf)
        assert "Dynamic WF" in agent.workflows

    def test_workflows_property_is_copy(self) -> None:
        from machina.workflows import Workflow

        agent = Agent()
        agent.register_workflow(Workflow(name="W1"))
        workflows = agent.workflows
        workflows["injected"] = Workflow(name="injected")
        # Original should not be mutated
        assert "injected" not in agent.workflows

    @pytest.mark.asyncio
    async def test_trigger_workflow(self) -> None:
        from machina.workflows import Step, Workflow

        wf = Workflow(name="Simple", steps=[Step("noop")])
        agent = Agent(workflows=[wf])
        result = await agent.trigger_workflow("Simple", {"asset_id": "P-201"})
        assert result.success is True
        assert result.workflow_name == "Simple"

    @pytest.mark.asyncio
    async def test_trigger_unknown_workflow_raises(self) -> None:
        from machina.exceptions import WorkflowError

        agent = Agent()
        with pytest.raises(WorkflowError, match="not registered"):
            await agent.trigger_workflow("Nonexistent")

    def test_sandbox_flag(self) -> None:
        agent = Agent(sandbox=True)
        assert agent.sandbox is True
        assert agent._engine.sandbox is True

    def test_sandbox_default_false(self) -> None:
        agent = Agent()
        assert agent.sandbox is False

    def test_sandbox_setter_propagates_true_to_engine(self) -> None:
        """Mutating ``agent.sandbox = True`` must update the engine.

        Regression for the --live propagation defect (report Luigi):
        the engine was constructed with a snapshot of sandbox at init
        time and never saw subsequent mutations on the Agent.
        """
        agent = Agent(sandbox=False)
        assert agent._engine.sandbox is False
        agent.sandbox = True
        assert agent.sandbox is True
        assert agent._engine.sandbox is True

    def test_sandbox_setter_propagates_false_to_engine(self) -> None:
        """Mutating ``agent.sandbox = False`` (the --live path) must update the engine."""
        agent = Agent(sandbox=True)
        assert agent._engine.sandbox is True
        agent.sandbox = False
        assert agent.sandbox is False
        assert agent._engine.sandbox is False

    def test_sandbox_setter_idempotent(self) -> None:
        """Setting sandbox to its current value is a no-op."""
        agent = Agent(sandbox=True)
        agent.sandbox = True
        assert agent.sandbox is True
        assert agent._engine.sandbox is True

    def test_sandbox_init_sets_connector_contextvar(self) -> None:
        """``Agent(sandbox=True)`` updates the ``_sandbox_mode`` contextvar.

        Otherwise the ``@sandbox_aware`` decorator on custom connector
        write methods (e.g. ``cmms.dispatch_field_team`` — names that
        don't match the engine's keyword heuristic) would bypass the
        sandbox gate even when the agent is in sandbox mode.
        """
        from machina.connectors.base import get_sandbox_mode

        Agent(sandbox=True)
        assert get_sandbox_mode() is True

    def test_sandbox_setter_updates_connector_contextvar(self) -> None:
        """The setter propagates to the contextvar, not only the engine."""
        from machina.connectors.base import get_sandbox_mode

        agent = Agent(sandbox=False)
        assert get_sandbox_mode() is False
        agent.sandbox = True
        assert get_sandbox_mode() is True
        agent.sandbox = False
        assert get_sandbox_mode() is False

    def test_system_prompt_in_sandbox_mode_announces_simulation(self) -> None:
        """The LLM must know it is in sandbox so it doesn't claim real writes."""
        from machina.agent.prompts import build_system_prompt

        prompt = build_system_prompt(sandbox=True)
        assert "SANDBOX mode is active" in prompt
        assert "no real data is modified" in prompt.lower() or "simulated" in prompt.lower()

    def test_system_prompt_in_live_mode_announces_real_consequences(self) -> None:
        from machina.agent.prompts import build_system_prompt

        prompt = build_system_prompt(sandbox=False)
        assert "LIVE mode is active" in prompt
        assert "real" in prompt.lower()

    @pytest.mark.asyncio
    async def test_build_domain_services_reapplies_sandbox_to_engine(self) -> None:
        """``_build_domain_services`` re-applies ``self._sandbox`` so a rebuilt
        engine cannot silently drift below the canonical Agent value.
        """
        agent = Agent(sandbox=True)
        # Simulate the engine being replaced (e.g. by a test fixture).
        agent._engine.sandbox = False
        await agent._build_domain_services()
        assert agent._engine.sandbox is True

    def test_confirmations_default_true(self) -> None:
        """Confirmations are on by default (unlike sandbox)."""
        agent = Agent()
        assert agent.confirmations is True

    def test_confirmations_flag_stored(self) -> None:
        """``Agent(confirmations=False)`` stores the value."""
        agent = Agent(confirmations=False)
        assert agent.confirmations is False

    def test_confirmations_setter_toggles_value(self) -> None:
        """The setter flips the agent-loop-local flag at runtime."""
        agent = Agent(confirmations=True)
        agent.confirmations = False
        assert agent.confirmations is False
        agent.confirmations = True
        assert agent.confirmations is True


# ---------------------------------------------------------------------------
# Path-leak sanitisation at the runtime boundary (regression for report-luigi U1)
# ---------------------------------------------------------------------------


class TestRuntimeContextPayloadSanitization:
    """End-to-end: the ``search_documents`` tool result strips paths."""

    @pytest.mark.asyncio
    async def test_search_documents_tool_strips_absolute_paths(self) -> None:
        """The tool result fed back to the LLM must contain basenames only."""
        from machina.connectors.capabilities import Capability

        # Mock document connector that returns chunks with absolute paths,
        # file URIs, and one bypass shape (file://) that earlier passed
        # through unsanitised.
        leaky_chunks = [
            DocumentChunk(
                content="Remove the four bolts on the bearing housing.",
                source=r"C:\Users\tedib\Desktop\manuals\pump_p201_manual.md",
                page=5,
            ),
            DocumentChunk(
                content="Apply LOTO before opening the casing.",
                source="/home/me/manuals/safety.md",
                page=2,
            ),
            DocumentChunk(
                content="Bypass attempt via file URI.",
                source="file:///C:/Users/tedib/secret.md",
                page=1,
            ),
        ]
        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({Capability.SEARCH_DOCUMENTS})
        mock_conn.search = AsyncMock(return_value=leaky_chunks)
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()

        agent = Agent(
            name="test",
            connectors=[mock_conn],
            channels=[],
            llm="openai:gpt-4o",  # never actually called in this test
        )

        result: list[dict[str, Any]] = await agent._execute_tool(
            "search_documents",
            {"query": "bearing replacement"},
        )

        assert isinstance(result, list)
        assert len(result) == 3
        sources = [r["source"] for r in result]
        assert sources == ["pump_p201_manual.md", "safety.md", "secret.md"]
        for r in result:
            assert "C:\\Users" not in r["source"]
            assert "/home/me" not in r["source"]
            assert "tedib" not in r["source"]
            assert "file://" not in r["source"]


# ---------------------------------------------------------------------------
# U4 — Runtime HITL gate (synchronous / CLI path)
# ---------------------------------------------------------------------------


class _RecordingConfirmer:
    """Async confirmer stub that records prompts and returns canned decisions."""

    def __init__(self, decisions: list[bool] | bool = True) -> None:
        self.prompts: list[str] = []
        self._decisions = decisions

    async def __call__(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        if isinstance(self._decisions, bool):
            return self._decisions
        idx = len(self.prompts) - 1
        return self._decisions[idx] if idx < len(self._decisions) else self._decisions[-1]


class _FakeLLMSingleCreate:
    """Requests create_work_order once, then returns text."""

    def __init__(self, args: dict[str, Any] | None = None) -> None:
        self.model = "fake:model"
        self._call_count = 0
        self._args = args or {
            "asset_id": "P-201",
            "type": "corrective",
            "priority": "high",
            "description": "Replace bearing",
        }

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Work order handled."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._call_count += 1
        if self._call_count == 1:
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(self._args)
            tc.id = "call_001"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Work order handled.", "tool_calls": None}


class _FakeLLMRewordedCreate:
    """Requests create_work_order twice with DIFFERENT descriptions (reworded)."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._call_count = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Done."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._call_count += 1
        descriptions = {1: "Replace bearing", 2: "Swap the worn bearing out"}
        if self._call_count in descriptions:
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(
                {
                    "asset_id": "P-201",
                    "type": "corrective",
                    "description": descriptions[self._call_count],
                }
            )
            tc.id = f"call_{self._call_count:03d}"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Done.", "tool_calls": None}


class TestHitlGateSyncPath:
    """U4: the synchronous confirmer gates mutating tool calls in _llm_loop."""

    @pytest.mark.asyncio
    async def test_confirm_true_executes_write(self) -> None:
        """AE1: confirmations on + confirmer True → write fires once."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RecordingConfirmer(True)
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        assert conn.create_calls == 1
        assert len(confirmer.prompts) == 1

    @pytest.mark.asyncio
    async def test_confirm_false_blocks_write(self) -> None:
        """AE1: confirmer False → connector NOT called; loop still responds."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RecordingConfirmer(False)
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        result = await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        assert conn.create_calls == 0
        assert isinstance(result, str)
        assert len(confirmer.prompts) == 1

    @pytest.mark.asyncio
    async def test_reworded_create_prompts_each_distinct(self) -> None:
        """AE2: a reworded create is a distinct proposal → fresh prompt; decline blocks it."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMRewordedCreate()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RecordingConfirmer([True, False])
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        # Two distinct proposals → two prompts; only the first (confirmed) wrote.
        assert len(confirmer.prompts) == 2
        assert conn.create_calls == 1

    @pytest.mark.asyncio
    async def test_confirmations_off_no_confirmer_call(self) -> None:
        """AE4 regression: confirmations off → write executes, confirmer untouched."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], confirmations=False)
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RecordingConfirmer(True)
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        assert conn.create_calls == 1
        assert confirmer.prompts == []

    @pytest.mark.asyncio
    async def test_sandbox_skips_gate(self) -> None:
        """Sandbox on + confirmations on → no confirmer call, no real write."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], sandbox=True)
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RecordingConfirmer(True)
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        assert conn.create_calls == 0
        assert confirmer.prompts == []

    @pytest.mark.asyncio
    async def test_programmatic_caller_no_confirmer_blocks_write(self) -> None:
        """No confirmer + confirmations on → fail-safe, write NOT executed."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1")
        assert conn.create_calls == 0

    @pytest.mark.asyncio
    async def test_programmatic_handle_message_full_blocks_write(self) -> None:
        """handle_message_full with confirmations on + no confirmer → no write."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201")
        assert conn.create_calls == 0

    @pytest.mark.asyncio
    async def test_decline_collapse_same_proposal(self) -> None:
        """An identical (tool, args) re-proposed in the same turn auto-declines."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMDoubleCreate()  # identical args twice
        await agent.start()
        confirmer = _RecordingConfirmer(False)
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        # Declined once; the verbatim re-proposal is auto-declined (no 2nd prompt).
        assert len(confirmer.prompts) == 1
        assert conn.create_calls == 0

    @pytest.mark.asyncio
    async def test_read_only_tool_never_gated(self) -> None:
        """search_assets (read-only) is never gated."""
        plant = _make_plant()
        agent = Agent(plant=plant, connectors=[_FakeConnector()])
        agent._llm = _FakeLLMWithToolCalls()  # calls search_assets
        await agent.start()
        confirmer = _RecordingConfirmer(True)
        messages = [{"role": "user", "content": "Tell me about P-201"}]
        result = await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        assert "P-201" in result
        assert confirmer.prompts == []

    @pytest.mark.asyncio
    async def test_workflow_gated_once_at_boundary(self) -> None:
        """execute_workflow is gated once; internal writes don't re-prompt."""
        from machina.workflows import Step, Workflow

        # Workflow whose step performs a create-work-order side effect
        # internally (via the CMMS connector action). The gate must fire only
        # at the execute_workflow boundary — the engine runs the internal write
        # through trigger_workflow, which never re-enters _llm_loop.
        wf = Workflow(
            name="AlarmToWO",
            steps=[
                Step(
                    name="open_wo",
                    action="cmms.create_work_order",
                    inputs={"asset_id": "P-201", "description": "Replace bearing"},
                )
            ],
        )
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn], workflows=[wf])

        class _WorkflowLLM:
            model = "fake:model"

            def __init__(self) -> None:
                self._n = 0

            async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
                return "Workflow launched."

            async def complete_with_tools(
                self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
            ) -> dict[str, Any]:
                self._n += 1
                if self._n == 1:
                    tc = MagicMock()
                    tc.function.name = "execute_workflow"
                    tc.function.arguments = json.dumps(
                        {"workflow_name": "AlarmToWO", "event": {"asset_id": "P-201"}}
                    )
                    tc.id = "wf1"
                    return {"content": "", "tool_calls": [tc]}
                return {"content": "Workflow launched.", "tool_calls": None}

        agent._llm = _WorkflowLLM()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RecordingConfirmer(True)
        messages = [{"role": "user", "content": "run the alarm workflow"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        # Exactly one confirmation at the workflow boundary.
        assert len(confirmer.prompts) == 1

    def test_confirmation_prompt_describes_create_work_order(self) -> None:
        """R6: the prompt names asset/type/priority/description concretely."""
        agent = Agent()
        prompt = agent._confirmation_prompt(
            "create_work_order",
            {
                "asset_id": "P-201",
                "type": "corrective",
                "priority": "high",
                "description": "Replace bearing",
            },
        )
        assert "P-201" in prompt
        assert "corrective" in prompt
        assert "high" in prompt
        assert "Replace bearing" in prompt

    def test_confirmation_prompt_describes_execute_workflow(self) -> None:
        """R6: workflow prompt names the workflow and summarises the event."""
        agent = Agent()
        prompt = agent._confirmation_prompt(
            "execute_workflow",
            {"workflow_name": "AlarmToWO", "event": {"asset_id": "P-201"}},
        )
        assert "AlarmToWO" in prompt
        assert "P-201" in prompt

    def test_confirmation_prompt_workflow_empty_event_fallback(self) -> None:
        """R6: a workflow with no event still yields a non-empty description."""
        agent = Agent()
        prompt = agent._confirmation_prompt("execute_workflow", {"workflow_name": "WF"})
        assert "WF" in prompt
        assert prompt.strip()


# ---------------------------------------------------------------------------
# U5 — Two-turn confirmation degrade for async channels
# ---------------------------------------------------------------------------


class _FakeLLMNarrate:
    """Narration-only fake LLM: never requests a tool, just returns text.

    Used to assert that a re-entered narration-only loop produces a narrated
    answer rather than a raw connector payload.
    """

    def __init__(self, text: str = "I created the work order for P-201.") -> None:
        self.model = "fake:model"
        self._text = text

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return self._text

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return {"content": self._text, "tool_calls": None}


class _FakeLLMReissueCreate:
    """Always re-issues create_work_order (verbatim or reworded) every call.

    Used to assert the narration-only re-entry suppresses a second write and a
    second confirmation prompt even when the model is eager.
    """

    def __init__(self, reworded: bool = False) -> None:
        self.model = "fake:model"
        self._reworded = reworded
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Narrated."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        desc = (
            "Swap the worn bearing out" if (self._reworded and self._n > 1) else "Replace bearing"
        )
        tc = MagicMock()
        tc.function.name = "create_work_order"
        tc.function.arguments = json.dumps(
            {"asset_id": "P-201", "type": "corrective", "description": desc}
        )
        tc.id = f"reissue_{self._n:03d}"
        return {"content": "", "tool_calls": [tc]}


class TestAffirmationGrammar:
    """U5/R5: deterministic affirmation/decline parse (no LLM)."""

    def test_affirmation_recognizes_en_and_it_tokens(self) -> None:
        agent = Agent()
        for tok in ("y", "yes", "ok", "okay", "confirm", "sì", "si", "conferma", "procedi", "vai"):
            assert agent._is_affirmation(tok), tok
            assert agent._is_affirmation(tok.upper()), tok
            assert agent._is_affirmation(f"  {tok}  "), tok

    def test_decline_recognizes_en_and_it_tokens(self) -> None:
        agent = Agent()
        for tok in ("n", "no", "annulla", "cancel", "stop", "abort"):
            assert agent._is_decline(tok), tok
            assert agent._is_decline(tok.upper()), tok

    def test_compound_message_is_neither(self) -> None:
        agent = Agent()
        assert not agent._is_affirmation("ok, but set priority high")
        assert not agent._is_decline("no thanks, check P-202 instead")
        # an unrelated message is neither
        assert not agent._is_affirmation("what is the status of P-201?")
        assert not agent._is_decline("what is the status of P-201?")


class TestTwoTurnConfirmation:
    """U5: propose → confirm → execute across two turns on async channels."""

    @pytest.mark.asyncio
    async def test_propose_stores_pending_and_does_not_write(self) -> None:
        """AE3: no confirmer + model proposes a write → confirmation question, no write."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        resp = await agent.handle_message_full(
            "create a WO for P-201", chat_id="c1", user_id="userA"
        )
        assert conn.create_calls == 0
        assert ("c1", "userA") in agent._pending_actions
        # The confirmation question (the prompt) surfaces in the response.
        assert "work order" in resp.text.lower() or "P-201" in resp.text

    @pytest.mark.asyncio
    async def test_affirmation_executes_and_narrates(self) -> None:
        """AE3: next message is an affirmation → write executes once, narrated answer."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        assert conn.create_calls == 0
        # Confirming message — swap the LLM for a narrator so the re-entry narrates.
        agent._llm = _FakeLLMNarrate()  # type: ignore[assignment]
        resp = await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        assert conn.create_calls == 1
        assert ("c1", "userA") not in agent._pending_actions
        # Narrated answer, not a raw model_dump payload.
        assert "work order" in resp.text.lower()
        assert "model_dump" not in resp.text

    @pytest.mark.asyncio
    async def test_confirmed_write_empty_narration_is_not_failure(self) -> None:
        """A confirmed write whose narration comes back empty must report success,
        not the generic 'try rephrasing / switch models' fallback (which could
        drive the user to retry and mint a duplicate write)."""
        from machina.agent.runtime import _EMPTY_RESPONSE_FALLBACK

        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        # Narrator returns nothing — the write already executed.
        agent._llm = _FakeLLMNarrate("")  # type: ignore[assignment]
        resp = await agent.handle_message_full("yes", chat_id="c1", user_id="userA")

        assert conn.create_calls == 1
        assert resp.is_fallback is True
        # Must NOT be the generic failure-flavoured fallback, and must not invite a retry.
        assert resp.text != _EMPTY_RESPONSE_FALLBACK
        assert "completed" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_unrelated_message_discards_pending(self) -> None:
        """AE3: an unrelated next message → pending discarded, processed normally, no write."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        # Unrelated follow-up; swap to a narrator so the normal path returns text.
        agent._llm = _FakeLLMNarrate("P-201 is a cooling water pump.")  # type: ignore[assignment]
        await agent.handle_message_full("what is P-201?", chat_id="c1", user_id="userA")
        assert conn.create_calls == 0
        assert ("c1", "userA") not in agent._pending_actions

    @pytest.mark.asyncio
    async def test_pending_survives_turn_chunk_reset(self) -> None:
        """Turn-survival: pending is NOT in _turn_chunks (reset each turn)."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        # _turn_chunks is popped at the end of the turn; the pending action lives elsewhere.
        assert "c1" not in agent._turn_chunks
        assert ("c1", "userA") in agent._pending_actions
        # A second call still finds it.
        agent._llm = _FakeLLMNarrate()  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        assert conn.create_calls == 1

    @pytest.mark.asyncio
    async def test_cross_user_isolation(self) -> None:
        """A pending action for userA is NOT confirmable by userB."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        # userB says "yes" on the same chat — must NOT execute userA's pending write.
        agent._llm = _FakeLLMNarrate()  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userB")
        assert conn.create_calls == 0
        # userA's pending is still intact.
        assert ("c1", "userA") in agent._pending_actions

    @pytest.mark.asyncio
    async def test_empty_user_id_withholds_write(self) -> None:
        """Empty (untrusted) user_id → write withheld: not stored, not executed."""
        import structlog

        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        events: list[dict[str, Any]] = []

        def _capture(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
            events.append(dict(event_dict))
            return event_dict

        structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
        try:
            await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="")
        finally:
            structlog.reset_defaults()
        assert any(e.get("event") == "write_withheld_anonymous" for e in events)
        # Fail-safe: nothing stored, nothing executed.
        assert ("c1", "") not in agent._pending_actions
        assert conn.create_calls == 0

    @pytest.mark.asyncio
    async def test_empty_user_id_returns_unconfirmable_result(self) -> None:
        """Empty user_id → the gated tool result carries unconfirmable=True."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        result = await agent._await_write_confirmation(
            "create_work_order", {"asset_id": "P-201"}, "c1", ""
        )
        assert result.get("unconfirmable") is True
        assert result.get("confirmation_required") is True
        assert ("c1", "") not in agent._pending_actions
        assert conn.create_calls == 0

    @pytest.mark.asyncio
    async def test_reentry_suppresses_verbatim_reissue(self) -> None:
        """Re-entry safety: a verbatim re-issue produces no 2nd write, no 2nd prompt."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        # On confirm, the re-entered loop's model keeps re-issuing the SAME write.
        agent._llm = _FakeLLMReissueCreate(reworded=False)  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        # The just-confirmed write executed exactly once; the re-issues collapse.
        assert conn.create_calls == 1
        assert ("c1", "userA") not in agent._pending_actions

    @pytest.mark.asyncio
    async def test_reentry_suppresses_reworded_reissue(self) -> None:
        """Re-entry safety: a reworded re-issue produces no 2nd write, no 2nd prompt."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        agent._llm = _FakeLLMReissueCreate(reworded=True)  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        # Mutating tools are suppressed during narration, so even a reworded
        # re-issue cannot write a duplicate.
        assert conn.create_calls == 1

    @pytest.mark.asyncio
    async def test_unrelated_proposal_supersedes_after_cancel(self) -> None:
        """A new proposal in a LATER turn supersedes the prior pending.

        The prior pending is first cancelled by the (non-affirmation) incoming
        message on the resume path, then the fresh turn proposes its own write.
        """
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate(  # type: ignore[assignment]
            {"asset_id": "P-201", "type": "corrective", "description": "first"}
        )
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        first = agent._pending_actions[("c1", "userA")]
        # A different proposal arrives before confirmation (separate turn).
        agent._llm = _FakeLLMSingleCreate(  # type: ignore[assignment]
            {"asset_id": "P-202", "type": "corrective", "description": "second"}
        )
        await agent.handle_message_full("create a WO for P-202", chat_id="c1", user_id="userA")
        second = agent._pending_actions[("c1", "userA")]
        assert first != second
        assert second[1]["asset_id"] == "P-202"

    @pytest.mark.asyncio
    async def test_expired_pending_not_executed_on_affirmation(self) -> None:
        """FIX 2: a pending older than the TTL is not executed by a later 'yes'."""
        import machina.agent.runtime as runtime_mod

        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        assert ("c1", "userA") in agent._pending_actions
        # Back-date the stored timestamp beyond the TTL.
        fn, args, prompt, _ts = agent._pending_actions[("c1", "userA")]
        stale_ts = time.monotonic() - runtime_mod._PENDING_ACTION_TTL_SECONDS - 1.0
        agent._pending_actions[("c1", "userA")] = (fn, args, prompt, stale_ts)
        # A later "yes" must NOT execute the stale write; it is processed fresh.
        agent._llm = _FakeLLMNarrate("Nothing pending.")  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        assert conn.create_calls == 0
        assert ("c1", "userA") not in agent._pending_actions

    @pytest.mark.asyncio
    async def test_fresh_pending_within_ttl_still_executes(self) -> None:
        """FIX 2: a pending within the TTL still executes on affirmation."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        agent._llm = _FakeLLMNarrate()  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        assert conn.create_calls == 1
        assert ("c1", "userA") not in agent._pending_actions


# ---------------------------------------------------------------------------
# FIX A — narration re-entry builds a VALID message sequence (no orphan tool)
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """Fake LLM that records every message list it receives.

    ``complete`` returns a narrated answer; ``complete_with_tools`` records the
    call and returns plain text (so it never drives a write). Used to assert the
    narration re-entry uses the no-tools ``complete`` path and never builds an
    orphan ``role:tool`` message.
    """

    def __init__(self, response: str = "I created the work order.") -> None:
        self.model = "fake:model"
        self._response = response
        self.complete_messages: list[list[dict[str, Any]]] = []
        self.complete_with_tools_messages: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.complete_messages.append([dict(m) for m in messages])
        return self._response

    async def complete_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.complete_with_tools_messages.append([dict(m) for m in messages])
        return {"content": self._response, "tool_calls": None}


def _assert_tool_message_contract(messages: list[dict[str, Any]]) -> None:
    """Every ``role:tool`` message must carry a ``tool_call_id`` and follow an
    assistant message that announced that id in its ``tool_calls``."""
    announced_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = getattr(tc, "id", None) if not isinstance(tc, dict) else tc.get("id")
                if tc_id:
                    announced_ids.add(tc_id)
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id")
            assert tcid, f"role:tool message has no tool_call_id: {msg!r}"
            assert tcid in announced_ids, (
                f"role:tool message {tcid!r} not preceded by an assistant tool_calls entry"
            )


class TestNarrationReentryContract:
    """FIX A: the two-turn narration pass must not emit an orphan role:tool."""

    @pytest.mark.asyncio
    async def test_narration_uses_complete_not_tool_loop(self) -> None:
        """Confirming a pending write narrates via the no-tools ``complete``
        path; no ``role:tool`` message lacking a ``tool_call_id`` is ever sent,
        and the write executes exactly once."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        assert conn.create_calls == 0

        rec = _RecordingLLM()
        agent._llm = rec  # type: ignore[assignment]
        resp = await agent.handle_message_full("yes", chat_id="c1", user_id="userA")

        # The write executed exactly once on the confirm path.
        assert conn.create_calls == 1
        # Narration went through the no-tools completion path.
        assert len(rec.complete_messages) == 1
        assert rec.complete_with_tools_messages == []
        # No orphan role:tool messages, and the contract holds on every message
        # list the LLM saw during narration.
        narration_msgs = rec.complete_messages[0]
        assert not any(m.get("role") == "tool" for m in narration_msgs)
        _assert_tool_message_contract(narration_msgs)
        # A narrated AgentResponse is returned (not a raw payload).
        assert isinstance(resp, AgentResponse)
        assert resp.text


# ---------------------------------------------------------------------------
# FIX D — _confirmation_prompt is concrete for EVERY mutating tool
# ---------------------------------------------------------------------------


class TestConfirmationPromptCompleteness:
    """Every MUTATING_TOOLS entry must produce a concrete (non-fallback) prompt."""

    def test_all_mutating_tools_have_concrete_prompt(self) -> None:
        from machina.llm.tools import MUTATING_TOOLS

        agent = Agent()
        # Minimal valid args per known mutating tool.
        minimal_args: dict[str, dict[str, Any]] = {
            "create_work_order": {
                "asset_id": "P-201",
                "type": "corrective",
                "priority": "high",
                "description": "Replace bearing",
            },
            "execute_workflow": {
                "workflow_name": "AlarmToWO",
                "event": {"asset_id": "P-201"},
            },
        }
        for name in MUTATING_TOOLS:
            assert name in minimal_args, (
                f"No minimal args registered for mutating tool {name!r} — add a "
                "branch in _confirmation_prompt AND an entry here so R6 stays concrete."
            )
            prompt = agent._confirmation_prompt(name, minimal_args[name])
            # The generic fallback is "Execute {name}?  • Arguments: ..." — a
            # concrete branch must NOT fall through to it.
            assert not prompt.startswith(f"Execute {name}?"), (
                f"_confirmation_prompt({name!r}) fell through to the generic "
                "fallback — add a concrete branch."
            )
            assert prompt.strip()


# ---------------------------------------------------------------------------
# FIX F — safety-coverage gaps
# ---------------------------------------------------------------------------


class _RaisingConfirmer:
    """Sync confirmer that raises (e.g. stdin EOF) — fail-closed contract."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.calls = 0
        self._exc = exc or EOFError("no input")

    async def __call__(self, prompt: str) -> bool:
        self.calls += 1
        raise self._exc


class _FakeLLMCreateThenSuccess:
    """Issues create_work_order twice with identical args (two iterations)."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Done."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        if self._n <= 2:
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(
                {"asset_id": "P-201", "type": "corrective", "description": "Replace bearing"}
            )
            tc.id = f"call_{self._n:03d}"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Done.", "tool_calls": None}


class TestSafetyCoverageGaps:
    """FIX F: fail-closed confirmer, error-not-memoised through the gate, and
    edge cases on the prompt/event helpers."""

    @pytest.mark.asyncio
    async def test_raising_confirmer_does_not_execute_write(self) -> None:
        """A confirmer that raises must NOT result in a write (fail-closed)."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]
        await agent.start()
        confirmer = _RaisingConfirmer()
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        # The exception may propagate OR be converted to a decline; either way
        # the write must NOT fire.
        with contextlib.suppress(EOFError):
            await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        assert confirmer.calls == 1
        assert conn.create_calls == 0

    @pytest.mark.asyncio
    async def test_error_result_not_memoised_through_gate(self) -> None:
        """On the confirmed path, an errored result is not memoised, so the
        write is attempted again on a verbatim re-issue (mirrors the
        confirmations=False test, but through the HITL gate)."""
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMCreateThenSuccess()  # type: ignore[assignment]
        await agent.start()

        calls = {"n": 0}

        async def fake_exec(name: str, args: dict[str, Any], *, chat_id: str = "default") -> Any:
            calls["n"] += 1
            return {"error": "transient"} if calls["n"] == 1 else {"id": "WO-OK"}

        agent._execute_tool = fake_exec  # type: ignore[assignment]
        confirmer = _RecordingConfirmer(True)
        messages = [{"role": "user", "content": "create a WO for P-201"}]
        await agent._llm_loop(messages, "chat1", confirmer=confirmer)
        # First confirmed call errored (not memoised) → the verbatim re-issue is
        # confirmed AND executed again.
        assert calls["n"] == 2
        assert len(confirmer.prompts) == 2

    def test_confirmation_prompt_generic_fallback_non_empty(self) -> None:
        """A future/unknown mutating tool still yields a concrete-ish prompt."""
        agent = Agent()
        prompt = agent._confirmation_prompt("some_future_tool", {"x": 1, "y": "z"})
        assert prompt.strip()
        assert "some_future_tool" in prompt
        # Arguments are rendered so the user sees what would happen.
        assert "x=" in prompt

    def test_confirmation_prompt_generic_fallback_no_args(self) -> None:
        agent = Agent()
        prompt = agent._confirmation_prompt("some_future_tool", {})
        assert prompt.strip()
        assert "some_future_tool" in prompt

    def test_summarize_event_edge_cases(self) -> None:
        """``_summarize_event`` returns a non-empty marker for None/empty/non-dict."""
        assert Agent._summarize_event(None).strip()
        assert Agent._summarize_event({}).strip()
        assert Agent._summarize_event("not a dict").strip()
        assert Agent._summarize_event([1, 2, 3]).strip()


# ---------------------------------------------------------------------------
# FIX 3 — a different second pending in one turn is rejected, first survives
# ---------------------------------------------------------------------------


class _FakeLLMTwoPendingProposals:
    """Proposes two DIFFERENT writes in a single turn (two loop iterations)."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Proposed."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        assets = {1: "P-201", 2: "P-202"}
        if self._n in assets:
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(
                {"asset_id": assets[self._n], "type": "corrective", "description": "fix"}
            )
            tc.id = f"call_{self._n:03d}"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Proposed.", "tool_calls": None}


class _FakeLLMIdenticalProposalsTwice:
    """Proposes the SAME write twice in a single turn (two loop iterations)."""

    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Proposed."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        if self._n in (1, 2):
            tc = MagicMock()
            tc.function.name = "create_work_order"
            tc.function.arguments = json.dumps(
                {"asset_id": "P-201", "type": "corrective", "description": "fix"}
            )
            tc.id = f"call_{self._n:03d}"
            return {"content": "", "tool_calls": [tc]}
        return {"content": "Proposed.", "tool_calls": None}


class TestPendingKeepFirst:
    """FIX 3: a multi-write turn keeps the FIRST pending; a different second
    proposal is rejected (logged) and does NOT overwrite. Confirming executes
    the first. An identical re-proposal is a no-op (single pending)."""

    @pytest.mark.asyncio
    async def test_second_different_proposal_rejected(self) -> None:
        import structlog

        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMTwoPendingProposals()  # type: ignore[assignment]
        await agent.start()

        events: list[dict[str, Any]] = []

        def _capture(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
            events.append(dict(event_dict))
            return event_dict

        structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
        try:
            await agent.handle_message_full(
                "create two work orders", chat_id="c1", user_id="userA"
            )
        finally:
            structlog.reset_defaults()

        assert any(e.get("event") == "pending_write_rejected_existing" for e in events)
        # Keep-first: the FIRST proposal owns the single slot; second is dropped.
        assert agent._pending_actions[("c1", "userA")][1]["asset_id"] == "P-201"

    @pytest.mark.asyncio
    async def test_confirming_executes_the_first(self) -> None:
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMTwoPendingProposals()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create two work orders", chat_id="c1", user_id="userA")
        agent._llm = _FakeLLMNarrate()  # type: ignore[assignment]
        await agent.handle_message_full("yes", chat_id="c1", user_id="userA")
        # The first (P-201) is the one that executes.
        assert conn.create_calls == 1
        assert conn.created_assets == ["P-201"]

    @pytest.mark.asyncio
    async def test_identical_reproposal_is_noop(self) -> None:
        conn = _CountingCreateWoConnector()
        agent = Agent(connectors=[conn])
        agent._llm = _FakeLLMIdenticalProposalsTwice()  # type: ignore[assignment]
        await agent.start()
        await agent.handle_message_full("create a WO for P-201", chat_id="c1", user_id="userA")
        # Exactly one pending action survives.
        assert ("c1", "userA") in agent._pending_actions
        assert agent._pending_actions[("c1", "userA")][1]["asset_id"] == "P-201"
        assert conn.create_calls == 0


# ---------------------------------------------------------------------------
# FIX F (listen wiring) — confirmer binding depends on sync-confirmation support
# ---------------------------------------------------------------------------


class _SyncConfirmChannel:
    """Async channel that DOES support synchronous confirmation."""

    capabilities: ClassVar[list[str]] = ["send_message"]

    def __init__(self, message_text: str = "create a WO for P-201") -> None:
        self._message_text = message_text
        self.confirmer_was_some: bool | None = None
        self.confirm_called = False

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def request_confirmation(self, chat_id: str | int, prompt: str) -> bool:
        self.confirm_called = True
        return True

    async def listen(self, handler: Any) -> None:
        class _Msg:
            pass

        msg = _Msg()
        msg.text = self._message_text  # type: ignore[attr-defined]
        msg.chat_id = "test-chat"  # type: ignore[attr-defined]
        msg.user_id = "userZ"  # type: ignore[attr-defined]
        await handler(msg)


class _AsyncOnlyChannel:
    """Async channel WITHOUT synchronous confirmation support."""

    capabilities: ClassVar[list[str]] = ["send_message"]

    def __init__(self, message_text: str = "hello") -> None:
        self._message_text = message_text

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def listen(self, handler: Any) -> None:
        class _Msg:
            pass

        msg = _Msg()
        msg.text = self._message_text  # type: ignore[attr-defined]
        msg.chat_id = "test-chat"  # type: ignore[attr-defined]
        msg.user_id = "userZ"  # type: ignore[attr-defined]
        await handler(msg)


class TestListenConfirmerWiring:
    """FIX F: listen() binds a non-None confirmer only for sync-capable channels,
    and forwards msg.user_id into handle_message_full."""

    @pytest.mark.asyncio
    async def test_sync_channel_binds_confirmer_and_forwards_user_id(self) -> None:
        conn = _CountingCreateWoConnector()
        channel = _SyncConfirmChannel()
        agent = Agent(connectors=[conn], channels=[channel])
        agent._llm = _FakeLLMSingleCreate()  # type: ignore[assignment]

        captured: dict[str, Any] = {}
        orig = agent.handle_message_full

        async def spy(text: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return await orig(text, **kwargs)

        agent.handle_message_full = spy  # type: ignore[assignment]
        await agent._run_async()

        assert captured.get("confirmer") is not None
        assert captured.get("user_id") == "userZ"
        # The bound confirmer routes through the channel's request_confirmation,
        # which returned True → the write executed.
        assert channel.confirm_called is True
        assert conn.create_calls == 1

    @pytest.mark.asyncio
    async def test_async_channel_leaves_confirmer_none(self) -> None:
        channel = _AsyncOnlyChannel()
        agent = Agent(channels=[channel])
        agent._llm = _FakeLLM("Hi there.")  # type: ignore[assignment]

        captured: dict[str, Any] = {}
        orig = agent.handle_message_full

        async def spy(text: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return await orig(text, **kwargs)

        agent.handle_message_full = spy  # type: ignore[assignment]
        await agent._run_async()

        assert captured.get("confirmer") is None
        assert captured.get("user_id") == "userZ"


# ---------------------------------------------------------------------------
# U7 — sole-egress gate (R8/R10)
# ---------------------------------------------------------------------------


class _FakeLLMNovelToolCallEachIteration:
    """Issues a DIFFERENT read each iteration and never answers on its own.

    Every iteration makes progress (a novel call), so no in-loop break fires
    and the loop genuinely exhausts ``max_iterations`` before force-finalizing
    via ``complete()`` — unlike :class:`_FakeLLMAlwaysToolCall`, whose verbatim
    repeat trips the no-progress break at iteration 2. ``final_text`` scripts
    what the forced ``complete()`` call returns (a clean answer, leaked
    tool-call JSON, or a ``<think>``-wrapped answer).
    """

    def __init__(self, final_text: str = "Exhausted-iterations final answer.") -> None:
        self.model = "fake:model"
        self._final_text = final_text
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return self._final_text

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        tc = MagicMock()
        tc.function.name = "search_assets"
        tc.function.arguments = json.dumps({"query": f"pump-{self._n}"})
        tc.id = f"call_{self._n:03d}"
        return {"content": "", "tool_calls": [tc]}


class _FakeLLMInterleavedDuplicateWrite:
    """Pairs a novel read with the SAME re-issued write, every iteration.

    The novel read keeps ``iteration_made_progress`` alive each round, so the
    duplicate-suppression LIMIT — not the no-progress guard — is what breaks
    the loop: the exit path the sole-egress matrix must drive distinctly.
    """

    def __init__(self) -> None:
        self.model = "fake:model"
        self._n = 0

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return "Suppression-limit final answer."

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._n += 1
        fresh = MagicMock()
        fresh.function.name = "search_assets"
        fresh.function.arguments = json.dumps({"query": f"area-{self._n}"})
        fresh.id = f"fresh_{self._n:03d}"
        write = MagicMock()
        write.function.name = "create_work_order"
        write.function.arguments = json.dumps(
            {"asset_id": "P-201", "type": "corrective", "description": "Replace bearing"}
        )
        write.id = f"write_{self._n:03d}"
        return {"content": "", "tool_calls": [fresh, write]}


class TestSoleEgressGate:
    """R8/R10 — ``_finalize_turn`` is the sole egress for agent-loop text.

    Each of the four ``_llm_loop`` exit paths (natural text-only return,
    no-progress break, duplicate-suppression limit, max-iterations exhaustion)
    is driven end-to-end through ``handle_message_full`` with a scripted fake.
    A spy wrapping the bound ``_finalize_turn`` records every gate pass; the
    matrix asserts the gate ran exactly once per turn and the
    ``AgentResponse`` the caller received IS the object the gate returned —
    sole egress asserted by a test, not left as a convention.
    """

    @staticmethod
    def _spy_finalize(agent: Agent) -> list[AgentResponse]:
        """Wrap the bound ``_finalize_turn`` to record every gate pass."""
        recorded: list[AgentResponse] = []
        original = agent._finalize_turn

        def wrapper(**kwargs: Any) -> AgentResponse:
            resp = original(**kwargs)
            recorded.append(resp)
            return resp

        agent._finalize_turn = wrapper  # type: ignore[method-assign]
        return recorded

    @pytest.mark.parametrize(
        ("make_llm", "make_connector", "agent_kwargs", "forced", "final_text"),
        [
            (
                lambda: _FakeLLM("Natural final answer."),
                _FakeConnector,
                {},
                False,
                "Natural final answer.",
            ),
            (
                _FakeLLMRepeatRead,
                _FakeConnector,
                {},
                True,
                "Final answer.",
            ),
            (
                _FakeLLMInterleavedDuplicateWrite,
                _CountingCreateWoConnector,
                {"confirmations": False},
                True,
                "Suppression-limit final answer.",
            ),
            (
                _FakeLLMNovelToolCallEachIteration,
                _FakeConnector,
                {},
                True,
                "Exhausted-iterations final answer.",
            ),
        ],
        ids=[
            "natural-return",
            "no-progress-break",
            "duplicate-suppression-limit",
            "max-iterations",
        ],
    )
    @pytest.mark.asyncio
    async def test_every_exit_path_passes_the_gate(
        self,
        make_llm: Any,
        make_connector: Any,
        agent_kwargs: dict[str, Any],
        forced: bool,
        final_text: str,
    ) -> None:
        from machina.agent.runtime import _PARTIAL_COMPLETENESS_HEDGE

        agent = Agent(plant=_make_plant(), connectors=[make_connector()], **agent_kwargs)
        agent._llm = make_llm()  # type: ignore[assignment]
        recorded = self._spy_finalize(agent)

        resp = await agent.handle_message_full("Tell me about P-201")

        # Exactly one gate pass for the turn, and the response the caller got
        # is the very object the gate produced — no side door.
        assert len(recorded) == 1
        assert resp is recorded[0]
        # Forced paths are hedged as partial (R1.4); the natural return is not.
        expected = final_text + (_PARTIAL_COMPLETENESS_HEDGE if forced else "")
        assert resp.text == expected
        assert resp.completeness == ("partial" if forced else "complete")

    @pytest.mark.asyncio
    async def test_forced_final_leaked_tool_call_is_suppressed(self) -> None:
        """Force-finalization returning tool-call JSON → the gate's leak backstop.

        Locks in the topology claim: even text produced by the forced
        ``complete()`` call cannot reach the user without passing the gate, so
        a leak there is suppressed exactly like one on the natural path.
        """
        from machina.agent.runtime import _TOOL_CALL_LEAK_FALLBACK

        leak = json.dumps({"name": "create_work_order", "arguments": {"asset_id": "P-201"}})
        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        agent._llm = _FakeLLMNovelToolCallEachIteration(final_text=leak)  # type: ignore[assignment]
        recorded = self._spy_finalize(agent)

        resp = await agent.handle_message_full("Tell me about P-201")

        assert len(recorded) == 1
        assert resp is recorded[0]
        assert resp.text == _TOOL_CALL_LEAK_FALLBACK
        assert resp.is_fallback is True
        assert "create_work_order" not in resp.text

    @pytest.mark.asyncio
    async def test_forced_final_think_block_never_reaches_user(self) -> None:
        """Force-finalization returning a ``<think>``-wrapped answer → scrubbed.

        The U7 scrub covers the force-finalization path too: the reasoning
        block never reaches the user, the surviving answer is delivered (with
        the forced-path hedge), and nothing falls back.
        """
        raw = "<think>secret chain of thought</think>The pump needs a new bearing."
        agent = Agent(plant=_make_plant(), connectors=[_FakeConnector()])
        agent._llm = _FakeLLMNovelToolCallEachIteration(final_text=raw)  # type: ignore[assignment]
        recorded = self._spy_finalize(agent)

        resp = await agent.handle_message_full("Tell me about P-201")

        assert len(recorded) == 1
        assert resp is recorded[0]
        assert "secret chain of thought" not in resp.text
        assert "<think" not in resp.text
        assert resp.text.startswith("The pump needs a new bearing.")
        assert resp.is_fallback is False
        assert resp.completeness == "partial"
