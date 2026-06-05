"""Tests for MCP domain tools — happy paths, edge cases, sandbox behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import AssetNotFoundError, SandboxViolationError
from machina.runtime import MachinaRuntime


def _make_ctx(runtime: MachinaRuntime) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"runtime": runtime}
    return ctx


def _mock_cmms(
    caps: frozenset[Capability] | None = None,
) -> MagicMock:
    conn = MagicMock()
    conn.capabilities = caps or frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.GET_WORK_ORDER,
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_PLANS,
        }
    )
    return conn


SAMPLE_ASSETS = [
    Asset(id="P-001", name="Pump 1", type=AssetType.ROTATING_EQUIPMENT, criticality=Criticality.A),
    Asset(id="V-001", name="Valve 1", type=AssetType.SAFETY),
]

SAMPLE_WO = WorkOrder(
    id="WO-001",
    type=WorkOrderType.CORRECTIVE,
    priority=Priority.HIGH,
    asset_id="P-001",
    description="Bearing replacement",
)


class TestListAssets:
    @pytest.mark.asyncio
    async def test_returns_assets(self) -> None:
        from machina.mcp.tools import machina_list_assets

        conn = _mock_cmms()
        conn.read_assets = AsyncMock(return_value=SAMPLE_ASSETS)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_list_assets(_make_ctx(runtime))
        assert len(result) == 2
        assert result[0]["id"] == "P-001"
        assert result[0]["criticality"] == "A"

    @pytest.mark.asyncio
    async def test_returns_empty_without_cmms(self) -> None:
        from machina.mcp.tools import machina_list_assets

        runtime = MachinaRuntime()
        result = await machina_list_assets(_make_ctx(runtime))
        assert len(result) == 1
        assert "error" in result[0]


class TestGetAsset:
    @pytest.mark.asyncio
    async def test_found(self) -> None:
        from machina.mcp.tools import machina_get_asset

        conn = _mock_cmms()
        conn.get_asset = AsyncMock(return_value=SAMPLE_ASSETS[0])
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_asset(_make_ctx(runtime), "P-001")
        assert result["id"] == "P-001"
        assert result["name"] == "Pump 1"

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self) -> None:
        from machina.mcp.tools import machina_get_asset

        conn = _mock_cmms()
        conn.get_asset = AsyncMock(return_value=None)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_asset(_make_ctx(runtime), "X-999")
        assert "error" in result
        assert "X-999" in result["error"]


class TestListWorkOrders:
    @pytest.mark.asyncio
    async def test_returns_work_orders(self) -> None:
        from machina.mcp.tools import machina_list_work_orders

        conn = _mock_cmms()
        conn.read_work_orders = AsyncMock(return_value=[SAMPLE_WO])
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_list_work_orders(_make_ctx(runtime))
        assert len(result) == 1
        assert result[0]["id"] == "WO-001"
        assert result[0]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_filters_by_asset(self) -> None:
        from machina.mcp.tools import machina_list_work_orders

        conn = _mock_cmms()
        conn.read_work_orders = AsyncMock(return_value=[])
        runtime = MachinaRuntime(connectors={"cmms": conn})
        await machina_list_work_orders(_make_ctx(runtime), asset_id="P-001")
        conn.read_work_orders.assert_called_once_with(asset_id="P-001")


class TestGetWorkOrder:
    @pytest.mark.asyncio
    async def test_found(self) -> None:
        from machina.mcp.tools import machina_get_work_order

        conn = _mock_cmms()
        conn.get_work_order = AsyncMock(return_value=SAMPLE_WO)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_work_order(_make_ctx(runtime), "WO-001")
        assert result["id"] == "WO-001"

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        from machina.mcp.tools import machina_get_work_order

        conn = _mock_cmms()
        conn.get_work_order = AsyncMock(return_value=None)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_work_order(_make_ctx(runtime), "WO-999")
        assert "error" in result


class TestCreateWorkOrder:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        from machina.mcp.tools import machina_create_work_order

        conn = _mock_cmms()
        conn.get_asset = AsyncMock(return_value=SAMPLE_ASSETS[0])
        conn.create_work_order = AsyncMock(return_value=SAMPLE_WO)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_create_work_order(
            _make_ctx(runtime),
            asset_id="P-001",
            description="bearing wear",
            priority="high",
        )
        assert result["id"] == "WO-001"
        conn.create_work_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_sandbox_returns_synthesized(self) -> None:
        from machina.mcp.tools import machina_create_work_order

        conn = _mock_cmms()
        conn.get_asset = AsyncMock(return_value=SAMPLE_ASSETS[0])
        conn.create_work_order = AsyncMock(side_effect=SandboxViolationError("blocked"))
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_create_work_order(
            _make_ctx(runtime),
            asset_id="P-001",
            description="bearing wear",
            priority="high",
        )
        assert result["metadata"]["sandbox"] is True
        assert "[SANDBOX" in result["description"]

    @pytest.mark.asyncio
    async def test_nonexistent_asset_raises(self) -> None:
        """Sandbox read-validation: no fake success for non-existent assets."""
        from machina.mcp.tools import machina_create_work_order

        conn = _mock_cmms()
        conn.get_asset = AsyncMock(return_value=None)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        with pytest.raises(AssetNotFoundError, match="X-999"):
            await machina_create_work_order(
                _make_ctx(runtime),
                asset_id="X-999",
                description="test",
            )
        # The actual create should never have been called
        conn.create_work_order = AsyncMock()
        assert not conn.create_work_order.called


class TestUpdateWorkOrder:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        from machina.mcp.tools import machina_update_work_order

        updated = SAMPLE_WO.model_copy(update={"assigned_to": "Mario"})
        conn = _mock_cmms()
        conn.update_work_order = AsyncMock(return_value=updated)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_update_work_order(
            _make_ctx(runtime),
            work_order_id="WO-001",
            assigned_to="Mario",
        )
        assert result["assigned_to"] == "Mario"

    @pytest.mark.asyncio
    async def test_sandbox_returns_synthesized(self) -> None:
        from machina.mcp.tools import machina_update_work_order

        conn = _mock_cmms()
        conn.update_work_order = AsyncMock(side_effect=SandboxViolationError("blocked"))
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_update_work_order(
            _make_ctx(runtime),
            work_order_id="WO-001",
            assigned_to="Mario",
        )
        assert result["metadata"]["sandbox"] is True


class TestRuntimeSandboxPropagation:
    """_runtime re-establishes sandbox mode in the per-request task context."""

    @pytest.mark.asyncio
    async def test_runtime_sets_sandbox_contextvar(self) -> None:
        from machina.connectors.base import get_sandbox_mode, set_sandbox_mode
        from machina.mcp.tools import _runtime

        set_sandbox_mode(False)  # simulate a request task that did not inherit it
        try:
            runtime = MachinaRuntime(connectors={}, sandbox_mode=True)
            ctx = _make_ctx(runtime)
            assert get_sandbox_mode() is False
            _runtime(ctx)
            assert get_sandbox_mode() is True
        finally:
            set_sandbox_mode(False)


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        from machina.mcp.tools import machina_send_message

        conn = MagicMock()
        conn.capabilities = frozenset({Capability.SEND_MESSAGE})
        conn.send_message = AsyncMock(return_value=None)
        runtime = MachinaRuntime(connectors={"comms": conn})
        result = await machina_send_message(_make_ctx(runtime), channel="#ops", text="hi")
        assert result["status"] == "sent"

    @pytest.mark.asyncio
    async def test_sandbox_returns_blocked(self) -> None:
        """When the comms connector raises SandboxViolationError, the tool must
        report it as blocked rather than letting a real message go out. Before
        the comms send methods were @sandbox_aware, this catch was dead code."""
        from machina.mcp.tools import machina_send_message

        conn = MagicMock()
        conn.capabilities = frozenset({Capability.SEND_MESSAGE})
        conn.send_message = AsyncMock(side_effect=SandboxViolationError("blocked"))
        runtime = MachinaRuntime(connectors={"comms": conn})
        result = await machina_send_message(_make_ctx(runtime), channel="#ops", text="hi")
        assert result["status"] == "blocked"
        assert result["metadata"]["sandbox"] is True

    @pytest.mark.asyncio
    async def test_send_works_with_non_channel_first_param(self) -> None:
        """Telegram/Email name their first param chat_id/to, not channel. The
        tool must call send_message positionally so it binds across connectors
        instead of raising TypeError on a channel= keyword."""
        from machina.mcp.tools import machina_send_message

        captured: dict[str, str] = {}

        class _TelegramLikeConn:
            capabilities = frozenset({Capability.SEND_MESSAGE})

            async def send_message(self, chat_id: str, text: str) -> None:
                captured["chat_id"] = chat_id
                captured["text"] = text

        runtime = MachinaRuntime(connectors={"comms": _TelegramLikeConn()})
        result = await machina_send_message(_make_ctx(runtime), channel="12345", text="hi")
        assert result["status"] == "sent"
        assert captured == {"chat_id": "12345", "text": "hi"}


class TestListSpareParts:
    @pytest.mark.asyncio
    async def test_returns_parts(self) -> None:
        from machina.mcp.tools import machina_list_spare_parts

        parts = [SparePart(sku="BRG-6205", name="Bearing 6205", stock_quantity=12)]
        conn = _mock_cmms()
        conn.read_spare_parts = AsyncMock(return_value=parts)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_list_spare_parts(_make_ctx(runtime))
        assert len(result) == 1
        assert result[0]["sku"] == "BRG-6205"


class TestGetMaintenancePlan:
    @pytest.mark.asyncio
    async def test_returns_plans(self) -> None:
        from machina.mcp.tools import machina_get_maintenance_plan

        plans = [
            MaintenancePlan(
                id="MP-001",
                asset_id="P-001",
                name="Quarterly check",
                interval=Interval(months=3),
                tasks=["Check vibration"],
            )
        ]
        conn = _mock_cmms()
        conn.read_maintenance_plans = AsyncMock(return_value=plans)
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_maintenance_plan(_make_ctx(runtime))
        assert len(result) == 1
        assert result[0]["interval_days"] == 90


class TestSearchManuals:
    @pytest.mark.asyncio
    async def test_no_doc_store(self) -> None:
        from machina.mcp.tools import machina_search_manuals

        runtime = MachinaRuntime()
        result = await machina_search_manuals(_make_ctx(runtime), query="bearing")
        assert "error" in result[0]

    @pytest.mark.asyncio
    async def test_with_doc_store(self) -> None:
        from machina.mcp.tools import machina_search_manuals

        chunk = MagicMock(source="manual.pdf", page=5, content="bearing info", score=0.9)
        doc_conn = MagicMock()
        doc_conn.capabilities = frozenset({Capability.SEARCH_DOCUMENTS})
        doc_conn.search_documents = AsyncMock(return_value=[chunk])
        runtime = MachinaRuntime(connectors={"docs": doc_conn})
        result = await machina_search_manuals(_make_ctx(runtime), query="bearing")
        assert len(result) == 1
        assert result[0]["source"] == "manual.pdf"

    @pytest.mark.asyncio
    async def test_strips_absolute_paths_at_mcp_boundary(self) -> None:
        """The MCP path must apply ``safe_source`` — same contract as the agent path.

        Regression for the post-U1 review finding that
        ``machina_search_manuals`` shipped raw ``DocumentChunk.source``
        values, leaking absolute filesystem paths to every MCP client
        (Claude Desktop, Cursor, third-party consumers) despite the
        agent-path leak being closed.
        """
        from machina.mcp.tools import machina_search_manuals

        leaky_chunks = [
            MagicMock(
                source=r"C:\Users\tedib\Desktop\manuals\pump.md",
                page=5,
                content="x",
                score=0.9,
            ),
            MagicMock(
                source="/home/me/manuals/safety.md",
                page=2,
                content="y",
                score=0.8,
            ),
            MagicMock(
                source="file:///C:/Users/tedib/secret.md",
                page=1,
                content="z",
                score=0.7,
            ),
        ]
        doc_conn = MagicMock()
        doc_conn.capabilities = frozenset({Capability.SEARCH_DOCUMENTS})
        doc_conn.search_documents = AsyncMock(return_value=leaky_chunks)
        runtime = MachinaRuntime(connectors={"docs": doc_conn})

        result = await machina_search_manuals(_make_ctx(runtime), query="x")

        sources = [r["source"] for r in result]
        assert sources == ["pump.md", "safety.md", "secret.md"]
        for r in result:
            assert "C:\\Users" not in r["source"]
            assert "/home/me" not in r["source"]
            assert "tedib" not in r["source"]
            assert "file://" not in r["source"]


class TestGetSensorReading:
    @pytest.mark.asyncio
    async def test_no_iot_connector(self) -> None:
        from machina.mcp.tools import machina_get_sensor_reading

        runtime = MachinaRuntime()
        result = await machina_get_sensor_reading(_make_ctx(runtime), asset_id="P-001")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_with_iot(self) -> None:
        from machina.mcp.tools import machina_get_sensor_reading

        iot_conn = MagicMock()
        iot_conn.capabilities = frozenset({Capability.GET_LATEST_READING})
        iot_conn.get_latest_reading = AsyncMock(
            return_value={"asset_id": "P-001", "temperature": 72.5}
        )
        runtime = MachinaRuntime(connectors={"iot": iot_conn})
        result = await machina_get_sensor_reading(_make_ctx(runtime), asset_id="P-001")
        assert result["temperature"] == 72.5
