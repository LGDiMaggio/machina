"""Tests for the GenericCmmsConnector."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorError


@pytest.fixture
def sample_data_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with sample CMMS data."""
    cmms_dir = tmp_path / "cmms"
    cmms_dir.mkdir()

    assets = [
        {
            "id": "P-201",
            "name": "Cooling Water Pump",
            "type": "rotating_equipment",
            "location": "Building A",
            "criticality": "A",
        },
        {
            "id": "COMP-301",
            "name": "Air Compressor",
            "type": "rotating_equipment",
            "location": "Building B",
            "criticality": "A",
        },
    ]
    (cmms_dir / "assets.json").write_text(json.dumps(assets))

    work_orders = [
        {
            "id": "WO-001",
            "type": "corrective",
            "priority": "high",
            "asset_id": "P-201",
            "description": "Bearing replacement",
            "failure_mode": "BEAR-WEAR-01",
        },
        {
            "id": "WO-002",
            "type": "preventive",
            "priority": "medium",
            "asset_id": "COMP-301",
            "description": "Filter replacement",
        },
    ]
    (cmms_dir / "work_orders.json").write_text(json.dumps(work_orders))

    spare_parts = [
        {
            "sku": "SKF-6310",
            "name": "Bearing 6310",
            "manufacturer": "SKF",
            "compatible_assets": ["P-201"],
            "stock_quantity": 4,
            "reorder_point": 2,
            "lead_time_days": 5,
            "unit_cost": 45.0,
            "warehouse_location": "W1",
        },
    ]
    (cmms_dir / "spare_parts.json").write_text(json.dumps(spare_parts))

    return cmms_dir


class TestGenericCmmsConnectorLocal:
    """Test GenericCmmsConnector in local data mode."""

    @pytest.mark.asyncio
    async def test_connect_with_data_dir(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        health = await conn.health_check()
        assert health.status.value == "healthy"
        await conn.disconnect()

    @pytest.mark.asyncio
    async def test_read_assets(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        assets = await conn.read_assets()
        assert len(assets) == 2
        assert assets[0].id == "P-201"
        assert assets[0].name == "Cooling Water Pump"

    @pytest.mark.asyncio
    async def test_get_asset(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        asset = await conn.get_asset("P-201")
        assert asset is not None
        assert asset.id == "P-201"

    @pytest.mark.asyncio
    async def test_get_missing_asset(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        asset = await conn.get_asset("NONEXISTENT")
        assert asset is None

    @pytest.mark.asyncio
    async def test_read_work_orders(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        wos = await conn.read_work_orders()
        assert len(wos) == 2

    @pytest.mark.asyncio
    async def test_read_work_orders_by_asset(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        wos = await conn.read_work_orders(asset_id="P-201")
        assert len(wos) == 1
        assert wos[0].asset_id == "P-201"

    @pytest.mark.asyncio
    async def test_create_work_order(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        wo = WorkOrder(
            id="WO-NEW",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="New test WO",
        )
        result = await conn.create_work_order(wo)
        assert result.id == "WO-NEW"
        # Verify it's persisted in memory
        all_wos = await conn.read_work_orders()
        assert len(all_wos) == 3

    @pytest.mark.asyncio
    async def test_read_spare_parts(self, sample_data_dir: Path) -> None:
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts(asset_id="P-201")
        assert len(parts) == 1
        assert parts[0].sku == "SKF-6310"

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        conn = GenericCmmsConnector(data_dir="/tmp/nonexistent")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_connect_no_source_raises(self) -> None:
        conn = GenericCmmsConnector()
        with pytest.raises(ConnectorError, match="Either"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_schema_mapping(self, tmp_path: Path) -> None:
        """Test that schema mapping renames fields correctly."""
        cmms_dir = tmp_path / "mapped"
        cmms_dir.mkdir()
        assets = [{"equipment_id": "X-1", "equipment_name": "Mapped Asset"}]
        (cmms_dir / "assets.json").write_text(json.dumps(assets))

        mapping = {
            "assets": {
                "equipment_id": "id",
                "equipment_name": "name",
            },
        }
        conn = GenericCmmsConnector(data_dir=cmms_dir, schema_mapping=mapping)
        await conn.connect()
        result = await conn.read_assets()
        assert len(result) == 1
        assert result[0].id == "X-1"
        assert result[0].name == "Mapped Asset"

    def test_capabilities(self) -> None:
        conn = GenericCmmsConnector()
        assert "read_assets" in conn.capabilities
        assert "create_work_order" in conn.capabilities
