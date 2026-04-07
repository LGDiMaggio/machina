"""Tests for the GenericCmmsConnector."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError


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
        {
            "sku": "FILTER-GA55",
            "name": "Air Filter GA55",
            "manufacturer": "Atlas Copco",
            "compatible_assets": ["COMP-301"],
            "stock_quantity": 2,
            "reorder_point": 1,
            "lead_time_days": 10,
            "unit_cost": 120.0,
            "warehouse_location": "W2",
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
    async def test_read_work_orders_by_status(self, sample_data_dir: Path) -> None:
        """Filter work orders by status."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        # Default status is "created", filter for it
        wos = await conn.read_work_orders(status="created")
        assert len(wos) == 2  # all WOs default to created status
        # Filter for a status that doesn't exist
        wos = await conn.read_work_orders(status="completed")
        assert len(wos) == 0

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
    async def test_read_spare_parts_by_sku(self, sample_data_dir: Path) -> None:
        """Filter spare parts by SKU."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts(sku="FILTER-GA55")
        assert len(parts) == 1
        assert parts[0].name == "Air Filter GA55"

    @pytest.mark.asyncio
    async def test_read_spare_parts_no_match(self, sample_data_dir: Path) -> None:
        """Filter spare parts for nonexistent asset."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts(asset_id="NONEXISTENT")
        assert len(parts) == 0

    @pytest.mark.asyncio
    async def test_read_spare_parts_all(self, sample_data_dir: Path) -> None:
        """Read all spare parts without filters."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        parts = await conn.read_spare_parts()
        assert len(parts) == 2

    @pytest.mark.asyncio
    async def test_read_maintenance_history(self, sample_data_dir: Path) -> None:
        """Maintenance history returns completed/closed WOs."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        # Default WOs are in "created" status, so history should be empty
        history = await conn.read_maintenance_history("P-201")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_read_maintenance_history_with_completed(self, sample_data_dir: Path) -> None:
        """Add a completed WO and verify it shows in maintenance history."""
        conn = GenericCmmsConnector(data_dir=sample_data_dir)
        await conn.connect()
        from machina.domain.work_order import WorkOrderStatus

        wo = WorkOrder(
            id="WO-DONE",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            asset_id="P-201",
            description="Completed bearing replacement",
        )
        # Transition through valid states to reach completed
        wo.transition_to(WorkOrderStatus.ASSIGNED)
        wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        wo.transition_to(WorkOrderStatus.COMPLETED)
        await conn.create_work_order(wo)
        history = await conn.read_maintenance_history("P-201")
        assert len(history) == 1
        assert history[0].id == "WO-DONE"

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


class TestGenericCmmsConnectorRest:
    """Test GenericCmmsConnector in REST mode."""

    @pytest.mark.asyncio
    async def test_rest_connect_no_api_key(self) -> None:
        """REST mode requires an API key."""
        conn = GenericCmmsConnector(url="http://example.com/api")
        with pytest.raises(ConnectorAuthError, match="API key"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_rest_connect_with_api_key(self) -> None:
        """REST mode connects successfully with an API key."""
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="test-key")
        await conn.connect()
        health = await conn.health_check()
        assert health.status.value == "healthy"
        assert health.details["mode"] == "rest"

    @pytest.mark.asyncio
    async def test_rest_read_assets_raises(self) -> None:
        """REST mode read_assets raises not-implemented error."""
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="test-key")
        await conn.connect()
        with pytest.raises(ConnectorError, match="not yet implemented"):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_rest_get_asset_raises(self) -> None:
        """REST mode get_asset raises not-implemented error."""
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="test-key")
        await conn.connect()
        with pytest.raises(ConnectorError, match="not yet implemented"):
            await conn.get_asset("P-201")

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="key")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        conn = GenericCmmsConnector(url="http://example.com/api", api_key="key")
        await conn.connect()
        await conn.disconnect()
        health = await conn.health_check()
        assert health.status.value == "unhealthy"
