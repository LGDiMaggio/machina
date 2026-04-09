"""Unit tests for UpKeepConnector.

Covers entity parsing helpers, priority mappings, and basic lifecycle
without network calls. For HTTP-level integration tests see
``tests/integration/test_upkeep_rest.py``.
"""

from __future__ import annotations

import pytest

from machina.connectors.cmms.upkeep import (
    UpKeepConnector,
    _parse_asset,
    _parse_maintenance_plan,
    _parse_spare_part,
    _parse_work_order,
    _reverse_priority,
)
from machina.domain.asset import Asset, AssetType
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import ConnectorAuthError, ConnectorError

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


class TestParseAsset:
    """Verify UpKeep JSON → Asset conversion."""

    def test_basic_fields(self) -> None:
        raw = {
            "id": "asset-1",
            "name": "Pump P-201",
            "category": "Rotating Equipment",
            "location": "Building A",
            "make": "Grundfos",
            "model": "CR 32-4",
            "serialNumber": "SN-12345",
            "parentAssetId": "parent-1",
        }
        asset = _parse_asset(raw)
        assert isinstance(asset, Asset)
        assert asset.id == "asset-1"
        assert asset.name == "Pump P-201"
        assert asset.type == AssetType.ROTATING_EQUIPMENT
        assert asset.location == "Building A"
        assert asset.manufacturer == "Grundfos"
        assert asset.model == "CR 32-4"
        assert asset.serial_number == "SN-12345"
        assert asset.parent == "parent-1"

    def test_unknown_category_defaults_to_rotating(self) -> None:
        raw = {"id": "a1", "name": "Widget", "category": "Unknown"}
        asset = _parse_asset(raw)
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_missing_fields_are_tolerated(self) -> None:
        raw = {"id": "a2"}
        asset = _parse_asset(raw)
        assert asset.id == "a2"
        assert asset.name == ""

    def test_extra_fields_stored_in_metadata(self) -> None:
        raw = {"id": "a3", "name": "X", "barcode": "BC-001"}
        asset = _parse_asset(raw)
        assert asset.metadata["barcode"] == "BC-001"


class TestParseWorkOrder:
    """Verify UpKeep JSON → WorkOrder conversion."""

    def test_corrective_work_order(self) -> None:
        raw = {
            "id": "wo-1",
            "title": "Fix leaking valve",
            "priority": 3,
            "status": "open",
            "assetId": "asset-1",
            "createdAt": "2025-06-01T10:00:00Z",
            "updatedAt": "2025-06-01T12:00:00Z",
        }
        wo = _parse_work_order(raw)
        assert isinstance(wo, WorkOrder)
        assert wo.id == "wo-1"
        assert wo.description == "Fix leaking valve"
        assert wo.priority == Priority.HIGH
        assert wo.status == WorkOrderStatus.CREATED
        assert wo.type == WorkOrderType.CORRECTIVE

    def test_preventive_work_order(self) -> None:
        raw = {
            "id": "wo-2",
            "title": "Monthly lubrication",
            "category": "preventive",
            "priority": 1,
            "status": "complete",
            "assetId": "asset-2",
            "createdAt": "2025-05-01T08:00:00Z",
            "updatedAt": "2025-05-15T14:00:00Z",
        }
        wo = _parse_work_order(raw)
        assert wo.type == WorkOrderType.PREVENTIVE
        assert wo.priority == Priority.LOW
        assert wo.status == WorkOrderStatus.COMPLETED

    def test_in_progress_status_mapping(self) -> None:
        raw = {
            "id": "wo-3",
            "title": "Repair",
            "priority": 2,
            "status": "in progress",
            "assetId": "a",
        }
        wo = _parse_work_order(raw)
        assert wo.status == WorkOrderStatus.IN_PROGRESS


class TestParseSparePart:
    """Verify UpKeep part JSON → SparePart conversion."""

    def test_basic_spare_part(self) -> None:
        raw = {
            "id": "part-1",
            "name": "Bearing SKF 6205",
            "quantity": 25,
            "cost": 42.50,
            "area": "Warehouse B",
        }
        sp = _parse_spare_part(raw)
        assert isinstance(sp, SparePart)
        assert sp.sku == "part-1"
        assert sp.name == "Bearing SKF 6205"
        assert sp.stock_quantity == 25
        assert sp.unit_cost == 42.50
        assert sp.warehouse_location == "Warehouse B"


class TestParseMaintenancePlan:
    """Verify UpKeep PM JSON → MaintenancePlan conversion."""

    def test_active_plan(self) -> None:
        raw = {
            "id": "pm-1",
            "title": "Weekly inspection",
            "assetId": "asset-1",
            "frequencyDays": 7,
            "status": "active",
            "tasks": ["Check pressure", "Inspect seals"],
        }
        plan = _parse_maintenance_plan(raw)
        assert isinstance(plan, MaintenancePlan)
        assert plan.id == "pm-1"
        assert plan.name == "Weekly inspection"
        assert plan.interval.days == 7
        assert plan.active is True
        assert len(plan.tasks) == 2


class TestReversePriority:
    """Verify Machina → UpKeep priority mapping."""

    def test_all_values(self) -> None:
        assert _reverse_priority(Priority.LOW) == 1
        assert _reverse_priority(Priority.MEDIUM) == 2
        assert _reverse_priority(Priority.HIGH) == 3
        assert _reverse_priority(Priority.EMERGENCY) == 4


# ---------------------------------------------------------------------------
# Connector instantiation & lifecycle (no HTTP)
# ---------------------------------------------------------------------------


class TestConnectorLifecycle:
    """Verify constructor defaults and pre-connect guards."""

    def test_default_url(self) -> None:
        conn = UpKeepConnector(api_key="tok")
        assert conn.url == "https://api.onupkeep.com"

    def test_custom_url(self) -> None:
        conn = UpKeepConnector(url="https://custom.onupkeep.com/", api_key="tok")
        assert conn.url == "https://custom.onupkeep.com"

    def test_capabilities(self) -> None:
        conn = UpKeepConnector(api_key="tok")
        assert "read_assets" in conn.capabilities
        assert "create_work_order" in conn.capabilities
        assert "read_maintenance_plans" in conn.capabilities

    @pytest.mark.asyncio
    async def test_connect_raises_without_api_key(self) -> None:
        conn = UpKeepConnector(api_key="")
        with pytest.raises(ConnectorAuthError, match="API key is required"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_read_before_connect_raises(self) -> None:
        conn = UpKeepConnector(api_key="tok")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_when_disconnected(self) -> None:
        conn = UpKeepConnector(api_key="tok")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"
