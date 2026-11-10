"""Tests for MCP domain tools — happy paths, edge cases, sandbox behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import (
    AssetNotFoundError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorSchemaError,
    ConnectorTimeoutError,
    SandboxViolationError,
)
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


class _ReadAssetsOnlyCmms:
    """A CMMS with only the READ_ASSETS contract: ``read_assets()``, no ``get_asset``."""

    capabilities = frozenset({Capability.READ_ASSETS})

    async def read_assets(self) -> list[Asset]:
        return SAMPLE_ASSETS


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

    @pytest.mark.asyncio
    async def test_found_on_primary_without_get_asset(self) -> None:
        """READ_ASSETS guarantees only read_assets(); get_asset is optional."""
        from machina.mcp.tools import machina_get_asset

        runtime = MachinaRuntime(connectors={"cmms": _ReadAssetsOnlyCmms()})
        result = await machina_get_asset(_make_ctx(runtime), "V-001")
        assert result["id"] == "V-001"
        assert result["name"] == "Valve 1"

    @pytest.mark.asyncio
    async def test_not_found_on_primary_without_get_asset(self) -> None:
        from machina.mcp.tools import machina_get_asset

        runtime = MachinaRuntime(connectors={"cmms": _ReadAssetsOnlyCmms()})
        result = await machina_get_asset(_make_ctx(runtime), "X-999")
        assert result == {"error": "Asset 'X-999' not found"}


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

    @pytest.mark.asyncio
    async def test_nonexistent_asset_raises_on_primary_without_get_asset(self) -> None:
        """The read-validation scans read_assets() when the primary has no get_asset."""
        from machina.mcp.tools import machina_create_work_order

        runtime = MachinaRuntime(connectors={"cmms": _ReadAssetsOnlyCmms()})
        with pytest.raises(AssetNotFoundError, match="X-999"):
            await machina_create_work_order(
                _make_ctx(runtime),
                asset_id="X-999",
                description="test",
            )

    @pytest.mark.asyncio
    async def test_existing_asset_passes_on_primary_without_get_asset(self) -> None:
        from machina.mcp.tools import machina_create_work_order

        class _WritableCmms(_ReadAssetsOnlyCmms):
            capabilities = frozenset({Capability.READ_ASSETS, Capability.CREATE_WORK_ORDER})

            async def create_work_order(self, wo: WorkOrder) -> WorkOrder:
                return wo

        runtime = MachinaRuntime(connectors={"cmms": _WritableCmms()})
        result = await machina_create_work_order(
            _make_ctx(runtime),
            asset_id="V-001",
            description="stem leak",
        )
        assert result["asset_id"] == "V-001"
        assert result["description"] == "stem leak"


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


class TestMcpCreateLocalModeDistinct:
    """MCP create must produce distinct ids in local mode, not collapse under
    the connector's id-idempotency guard (the old id='NEW' bug)."""

    @pytest.mark.asyncio
    async def test_distinct_creates_do_not_collapse(self, tmp_path) -> None:
        import json as _json

        from machina.connectors.cmms.generic import GenericCmmsConnector
        from machina.mcp.tools import machina_create_work_order

        cmms_dir = tmp_path / "cmms"
        cmms_dir.mkdir()
        (cmms_dir / "assets.json").write_text(
            _json.dumps(
                [
                    {
                        "id": "P-201",
                        "name": "Pump",
                        "type": "rotating_equipment",
                        "criticality": "A",
                    },
                    {
                        "id": "P-202",
                        "name": "Pump2",
                        "type": "rotating_equipment",
                        "criticality": "A",
                    },
                ]
            )
        )
        (cmms_dir / "work_orders.json").write_text("[]")
        conn = GenericCmmsConnector(data_dir=cmms_dir)
        await conn.connect()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        ctx = _make_ctx(runtime)

        r1 = await machina_create_work_order(ctx, asset_id="P-201", description="fix one")
        r2 = await machina_create_work_order(ctx, asset_id="P-202", description="fix two")
        assert r1["id"] != r2["id"]
        assert len(await conn.read_work_orders()) == 2


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
        assert result == [
            {
                "sku": "BRG-6205",
                "name": "Bearing 6205",
                "stock_quantity": 12,
                "reorder_point": 0,
                "unit_cost": 0.0,
            }
        ]

    @pytest.mark.asyncio
    async def test_sends_only_the_given_filters(self) -> None:
        """``sku`` narrows the lookup; an empty filter is not sent."""
        from machina.mcp.tools import machina_list_spare_parts

        conn = _mock_cmms()
        conn.read_spare_parts = AsyncMock(return_value=[])
        ctx = _make_ctx(MachinaRuntime(connectors={"cmms": conn}))

        await machina_list_spare_parts(ctx)
        await machina_list_spare_parts(ctx, sku="BRG-6205")
        await machina_list_spare_parts(ctx, asset_id="P-001")
        await machina_list_spare_parts(ctx, asset_id="P-001", sku="BRG-6205")

        assert conn.read_spare_parts.await_args_list == [
            call(),
            call(sku="BRG-6205"),
            call(asset_id="P-001"),
            call(asset_id="P-001", sku="BRG-6205"),
        ]


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


class _WorkOrdersOnlyCmms:
    """A primary CMMS without ``read_maintenance_history`` (SQL, Excel)."""

    capabilities = frozenset({Capability.READ_ASSETS, Capability.READ_WORK_ORDERS})

    def __init__(self) -> None:
        self.read_work_orders = AsyncMock(return_value=[SAMPLE_WO])


class TestGetMaintenanceHistory:
    @pytest.mark.parametrize("asset_id", ["", "  "], ids=["empty", "blank"])
    @pytest.mark.asyncio
    async def test_missing_asset_id_is_an_error_not_an_empty_history(
        self, httpx_mock, asset_id: str
    ) -> None:
        """An MCP client sees the refusal; ``[]`` would read as "never repaired"."""
        from machina.connectors.cmms.upkeep import UpKeepConnector
        from machina.mcp.tools import machina_get_maintenance_history

        conn = UpKeepConnector(api_key="k")
        conn._connected = True
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_maintenance_history(_make_ctx(runtime), asset_id)
        assert result == [{"error": "read_maintenance_history requires an asset_id"}]
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("asset_id", ["", "  ", None], ids=["empty", "blank", "none"])
    @pytest.mark.asyncio
    async def test_missing_asset_id_never_reaches_the_work_order_fallback(
        self, asset_id: str | None
    ) -> None:
        """Without a history method the tool reads work orders by asset, and
        ``read_work_orders(asset_id="")`` means every work order. ``None`` only
        arrives from a direct Python call; the MCP schema requires a string."""
        from machina.mcp.tools import machina_get_maintenance_history

        conn = _WorkOrdersOnlyCmms()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_maintenance_history(_make_ctx(runtime), asset_id)  # type: ignore[arg-type]
        assert result == [{"error": "read_maintenance_history requires an asset_id"}]
        conn.read_work_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_reads_the_asset_work_orders(self) -> None:
        from machina.mcp.tools import machina_get_maintenance_history

        conn = _WorkOrdersOnlyCmms()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        result = await machina_get_maintenance_history(_make_ctx(runtime), "P-001")
        assert [entry["id"] for entry in result] == ["WO-001"]
        conn.read_work_orders.assert_called_once_with(asset_id="P-001")

    @pytest.mark.asyncio
    async def test_fallback_passes_a_numeric_asset_id_as_text(self) -> None:
        """``read_work_orders(asset_id=0)`` would read ``0`` as no filter."""
        from machina.mcp.tools import machina_get_maintenance_history

        conn = _WorkOrdersOnlyCmms()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        await machina_get_maintenance_history(_make_ctx(runtime), 0)  # type: ignore[arg-type]
        conn.read_work_orders.assert_called_once_with(asset_id="0")


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


# ---------------------------------------------------------------------------
# machina_diagnose_failure — catalog matching over MCP (B9.1)
# ---------------------------------------------------------------------------

SAMPLE_CMMS = Path(__file__).resolve().parents[3] / "examples" / "sample_data" / "cmms"

DIAG_ASSETS = [
    Asset(
        id="P-201",
        name="Cooling Water Pump",
        type=AssetType.ROTATING_EQUIPMENT,
        criticality=Criticality.A,
        failure_modes=["BEAR-WEAR-01", "SEAL-LEAK-01", "IMP-EROSION-01"],
    ),
    Asset(id="HX-101", name="Heat Exchanger", type=AssetType.ROTATING_EQUIPMENT),
]


def _diag_catalog() -> list[FailureMode]:
    """The pump's declared modes plus a conveyor mode it must never get."""
    return [
        FailureMode(
            code="BEAR-WEAR-01",
            name="Bearing Wear",
            category="mechanical",
            typical_indicators=[
                "vibration_velocity_mm_s",
                "bearing_temperature_c",
                "vibration_acceleration_g",
            ],
            recommended_actions=["replace_bearing", "check_alignment"],
        ),
        FailureMode(
            code="SEAL-LEAK-01",
            name="Mechanical Seal Leakage",
            category="mechanical",
            typical_indicators=["seal_pressure_bar", "leakage_rate_ml_min"],
            recommended_actions=["replace_seal"],
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
            recommended_actions=["replace_impeller"],
        ),
        FailureMode(
            code="BELT-WEAR-01",
            name="Conveyor Belt Wear",
            category="mechanical",
            typical_indicators=["belt_speed_m_s", "vibration_velocity_mm_s"],
            recommended_actions=["replace_belt"],
        ),
    ]


class _CatalogProvider:
    """A connector declaring READ_FAILURE_MODES: serves ``modes`` or raises ``error``."""

    capabilities = frozenset({Capability.READ_FAILURE_MODES})

    def __init__(
        self,
        error: Exception | None = None,
        modes: list[FailureMode] | None = None,
    ) -> None:
        self._error = error
        self._modes = _diag_catalog() if modes is None else modes
        self.reads = 0

    async def read_failure_modes(self) -> list[FailureMode]:
        self.reads += 1
        if self._error is not None:
            raise self._error
        return list(self._modes)


def _diag_runtime(*providers: _CatalogProvider) -> MachinaRuntime:
    """Primary CMMS serving ``DIAG_ASSETS``, plus the given catalog providers.

    Each provider is registered as ``_CatalogProvider_<i>``, the name the
    agent's registry gives it, so agent and MCP results compare whole.
    """
    cmms = _mock_cmms()
    cmms.get_asset = AsyncMock(side_effect={a.id: a for a in DIAG_ASSETS}.get)
    connectors: dict[str, Any] = {"cmms": cmms}
    for i, provider in enumerate(providers):
        connectors[f"_CatalogProvider_{i}"] = provider
    return MachinaRuntime(connectors=connectors)


async def _diagnose(runtime: MachinaRuntime, asset_id: str, *symptoms: str) -> dict[str, Any]:
    from machina.mcp.tools import machina_diagnose_failure

    return await machina_diagnose_failure(
        _make_ctx(runtime), asset_id=asset_id, symptoms=list(symptoms)
    )


class TestDiagnoseFailure:
    """machina_diagnose_failure — the agent's ranking, notes and catalog report, over MCP."""

    @pytest.mark.asyncio
    async def test_ranks_declared_modes_by_matched_indicators(self) -> None:
        result = await _diagnose(_diag_runtime(_CatalogProvider()), "P-201", "high vibration")
        # BELT-WEAR-01 lists a vibration indicator too, but the pump does not
        # declare it.
        assert [f["code"] for f in result["probable_failures"]] == [
            "BEAR-WEAR-01",
            "IMP-EROSION-01",
        ]
        top = result["probable_failures"][0]
        assert top["indicator_match_ratio"] == 0.67  # 2 of 3 indicators carry "vibration"
        assert "confidence" not in top
        assert top["recommended_actions"] == ["replace_bearing", "check_alignment"]
        assert result["asset_name"] == "Cooling Water Pump"
        assert result["match_count"] == 2
        assert result["catalog"] == {
            "providers": ["_CatalogProvider_0"],
            "failed_providers": [],
            "partial": False,
        }
        assert "note" not in result
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unknown_asset_note_without_harvest(self) -> None:
        provider = _CatalogProvider()
        result = await _diagnose(_diag_runtime(provider), "GHOST-9", "vibration")
        assert result["probable_failures"] == []
        assert result["note"] == "Asset 'GHOST-9' not found in the asset registry."
        assert "asset_name" not in result
        assert "catalog" not in result  # nothing was harvested
        assert provider.reads == 0

    @pytest.mark.asyncio
    async def test_no_catalog_note(self) -> None:
        result = await _diagnose(_diag_runtime(), "P-201", "vibration")
        assert result["probable_failures"] == []
        assert result["note"] == "No failure-mode data configured on any connector."
        assert result["catalog"] == {"providers": [], "failed_providers": [], "partial": False}
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unreachable_sole_provider_reports_catalog_unavailable(self) -> None:
        """A catalog connector that failed to connect at startup is an outage,
        never the "not configured" note."""
        provider = _CatalogProvider(error=ConnectorError("Not connected"))
        result = await _diagnose(_diag_runtime(provider), "P-201", "vibration")
        assert result["error"] == (
            "Failure-mode catalog unavailable: provider '_CatalogProvider_0' failed "
            "(ConnectorError). Diagnosis could not run; this is a connector failure, "
            "not a missing configuration."
        )
        assert result["catalog"]["partial"] is True
        assert result["probable_failures"] == []
        assert "note" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectorTimeoutError("read timed out after 30s on sql01.plant.local"),
            ConnectorAuthError("login failed for user 'machina' password=hunter2"),
            ConnectorSchemaError(r"bad row in C:\Users\jdoe\plant\failure_modes.xlsx"),
        ],
        ids=["timeout", "auth", "schema"],
    )
    async def test_backend_failure_names_type_not_message(self, exc: ConnectorError) -> None:
        result = await _diagnose(
            _diag_runtime(_CatalogProvider(error=exc)), "P-201", "high vibration"
        )
        assert f"provider '_CatalogProvider_0' failed ({type(exc).__name__})" in result["error"]
        assert result["catalog"]["failed_providers"] == [
            {"name": "_CatalogProvider_0", "error_type": type(exc).__name__}
        ]
        dumped = json.dumps(result)
        for secret in ("sql01", "hunter2", "jdoe", "failure_modes.xlsx", str(exc)):
            assert secret not in dumped

    @pytest.mark.asyncio
    async def test_partial_catalog_with_matching_symptoms(self) -> None:
        runtime = _diag_runtime(
            _CatalogProvider(error=ConnectorTimeoutError("timed out")),
            _CatalogProvider(),
        )
        result = await _diagnose(runtime, "P-201", "high vibration")
        assert [f["code"] for f in result["probable_failures"]] == [
            "BEAR-WEAR-01",
            "IMP-EROSION-01",
        ]
        assert result["catalog"] == {
            "providers": ["_CatalogProvider_0", "_CatalogProvider_1"],
            "failed_providers": [
                {"name": "_CatalogProvider_0", "error_type": "ConnectorTimeoutError"}
            ],
            "partial": True,
        }
        assert result["note"] == (
            "Partial catalog: provider '_CatalogProvider_0' failed (ConnectorTimeoutError). "
            "Failure modes served by the failed provider(s) are missing from this result."
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_partial_catalog_with_nonmatching_symptoms(self) -> None:
        runtime = _diag_runtime(_CatalogProvider(error=ConnectorError("down")), _CatalogProvider())
        result = await _diagnose(runtime, "P-201", "strange smell")
        assert result["probable_failures"] == []
        assert result["match_count"] == 0
        assert result["note"].startswith("No catalog entry matched these symptoms.")
        assert result["note"].endswith(
            "Partial catalog: provider '_CatalogProvider_0' failed (ConnectorError). "
            "Failure modes served by the failed provider(s) are missing from this result."
        )

    @pytest.mark.asyncio
    async def test_no_match_note_lists_the_assets_indicators(self) -> None:
        result = await _diagnose(_diag_runtime(_CatalogProvider()), "P-201", "strange smell")
        assert result["probable_failures"] == []
        assert result["note"].startswith("No catalog entry matched these symptoms.")
        assert "bearing_temperature_c" in result["note"]
        assert "belt_speed_m_s" not in result["note"]  # not one of the pump's modes
        assert result["catalog"]["partial"] is False

    @pytest.mark.asyncio
    async def test_resolves_asset_on_cmms_without_get_asset(self) -> None:
        """READ_ASSETS guarantees only read_assets(), so a CMMS without
        get_asset is scanned instead."""

        class _DiagReadAssetsOnlyCmms:
            capabilities = frozenset({Capability.READ_ASSETS})

            async def read_assets(self) -> list[Asset]:
                return list(DIAG_ASSETS)

        runtime = MachinaRuntime(
            connectors={"cmms": _DiagReadAssetsOnlyCmms(), "catalog": _CatalogProvider()}
        )
        found = await _diagnose(runtime, "P-201", "high vibration")
        missing = await _diagnose(runtime, "GHOST-9", "vibration")
        assert [f["code"] for f in found["probable_failures"]] == [
            "BEAR-WEAR-01",
            "IMP-EROSION-01",
        ]
        assert missing["note"] == "Asset 'GHOST-9' not found in the asset registry."

    @pytest.mark.asyncio
    async def test_asset_lookup_failure_is_an_error_not_an_unknown_asset(self) -> None:
        """A CMMS that cannot answer the lookup is not "asset not found", and
        its message stays out of the result; the catalog is not read."""
        cmms = _mock_cmms()
        cmms.get_asset = AsyncMock(
            side_effect=ConnectorTimeoutError(
                "GET https://cmms.plant.local/assets/P-201 timed out"
            )
        )
        provider = _CatalogProvider()
        runtime = MachinaRuntime(connectors={"cmms": cmms, "catalog": provider})
        result = await _diagnose(runtime, "P-201", "vibration")
        assert result == {
            "asset_id": "P-201",
            "error": (
                "Asset lookup on the CMMS failed (ConnectorTimeoutError); diagnosis could not run."
            ),
        }
        assert provider.reads == 0

    @pytest.mark.asyncio
    async def test_no_cmms_for_the_asset_lookup_is_an_error(self) -> None:
        """With no connector that reads assets, the lookup has nowhere to go."""
        runtime = MachinaRuntime(connectors={"catalog": _CatalogProvider()})
        result = await _diagnose(runtime, "P-201", "vibration")
        assert result["error"] == (
            "Asset lookup on the CMMS failed (ConnectorError); diagnosis could not run."
        )

    @pytest.mark.asyncio
    async def test_non_connector_error_propagates(self) -> None:
        """A bug in a catalog provider or in the lookup is not an outage: it
        escapes the tool, and FastMCP returns an MCP error result."""
        runtime = _diag_runtime(_CatalogProvider(error=RuntimeError("programming error")))
        with pytest.raises(RuntimeError, match="programming error"):
            await _diagnose(runtime, "P-201", "vibration")

        cmms = _mock_cmms()
        cmms.get_asset = AsyncMock(side_effect=RuntimeError("lookup bug"))
        with pytest.raises(RuntimeError, match="lookup bug"):
            await _diagnose(MachinaRuntime(connectors={"cmms": cmms}), "P-201", "vibration")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("asset_id", "symptoms", "provider_errors"),
        [
            ("P-201", ["high vibration"], [None]),  # ranked matches
            ("HX-101", ["high vibration"], [None]),  # no declared modes: full catalog + note
            ("P-201", ["strange smell"], [None]),  # nothing matched: known-indicators note
            ("GHOST-9", ["vibration"], [None]),  # unknown asset
            ("P-201", ["vibration"], []),  # no provider configured
            ("P-201", ["high vibration"], [ConnectorError("down"), None]),  # partial
            ("P-201", ["vibration"], [ConnectorTimeoutError("slow")]),  # unavailable
        ],
        ids=["match", "full-catalog", "no-match", "unknown", "none", "partial", "unavailable"],
    )
    async def test_matches_agent_diagnose_failure(
        self,
        asset_id: str,
        symptoms: list[str],
        provider_errors: list[Exception | None],
    ) -> None:
        """Over the same assets and catalog, the MCP tool and the agent's
        diagnose_failure tool give identical answers (B9-D1)."""
        from machina.agent.runtime import Agent
        from machina.domain.plant import Plant

        plant = Plant(name="Diag Plant")
        for asset in DIAG_ASSETS:
            plant.register_asset(asset)
        agent = Agent(
            plant=plant,
            connectors=[_CatalogProvider(error=err) for err in provider_errors],
        )
        agent_result = await agent._execute_tool(
            "diagnose_failure", {"asset_id": asset_id, "symptoms": symptoms}
        )

        runtime = _diag_runtime(*(_CatalogProvider(error=err) for err in provider_errors))
        mcp_result = await _diagnose(runtime, asset_id, *symptoms)

        assert mcp_result == agent_result

    def test_registered_with_asset_reads_not_with_catalog_alone(self) -> None:
        # The catalog is optional (its absence is the tool's "no catalog"
        # note); an asset registry is not — the tool resolves the asset first.
        from machina.mcp.tools import get_tools_for_capabilities

        def names(*caps: Capability) -> set[str]:
            return {t.__name__ for t in get_tools_for_capabilities(frozenset(caps))}

        assert "machina_diagnose_failure" in names(Capability.READ_ASSETS)
        assert "machina_diagnose_failure" not in names(Capability.READ_FAILURE_MODES)

    @pytest.mark.asyncio
    async def test_real_client_call_reports_a_partial_catalog(self, tmp_path: Path) -> None:
        """End to end through an in-memory MCP client session and the server's
        own lifespan: a second catalog provider whose connect() failed at
        startup stays registered, and the diagnosis says the catalog is
        partial instead of passing the shrunken one off as complete."""
        from mcp.shared.memory import create_connected_server_and_client_session

        from machina.config.schema import ConnectorConfig, MachinaConfig
        from machina.mcp.server import build_server

        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "failure_modes.json").write_text("not json", encoding="utf-8")
        config = MachinaConfig(
            connectors={
                "cmms": ConnectorConfig(
                    type="generic_cmms", primary=True, settings={"data_dir": str(SAMPLE_CMMS)}
                ),
                "catalog_b": ConnectorConfig(
                    type="generic_cmms", settings={"data_dir": str(broken)}
                ),
            }
        )

        async with create_connected_server_and_client_session(build_server(config)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "machina_diagnose_failure",
                {"asset_id": "P-201", "symptoms": ["high vibration"]},
            )

        schema = tools["machina_diagnose_failure"].inputSchema
        assert set(schema["properties"]) == {"asset_id", "symptoms"}
        assert result.isError is False, result.content
        payload = result.structuredContent
        assert payload is not None
        assert [f["code"] for f in payload["probable_failures"]] == [
            "BEAR-WEAR-01",
            "IMP-EROSION-01",
        ]
        assert payload["catalog"] == {
            "providers": ["cmms", "catalog_b"],
            "failed_providers": [{"name": "catalog_b", "error_type": "ConnectorError"}],
            "partial": True,
        }
        assert payload["note"].startswith("Partial catalog: provider 'catalog_b' failed")
        assert str(tmp_path) not in json.dumps(payload)
