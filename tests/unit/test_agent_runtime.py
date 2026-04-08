"""Tests for the Agent runtime."""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.agent.runtime import Agent
from machina.connectors.docs.document_store import DocumentChunk
from machina.domain.asset import Asset, AssetType, Criticality
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


class _FakeDocConnector:
    """Connector stub for document search."""

    capabilities: ClassVar[list[str]] = ["search_documents"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def search(self, query: str, **kwargs: Any) -> list[DocumentChunk]:
        return [
            DocumentChunk(
                content="Pump P-201 bearing replacement procedure",
                source="manual.txt",
                page=1,
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
        assert agent._llm.model == "openai:gpt-4o"

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
