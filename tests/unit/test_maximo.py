"""Unit tests for MaximoConnector.

Covers entity parsing helpers, priority/status mappings, and basic
lifecycle without network calls.
"""

from __future__ import annotations

import pytest

from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.maximo import (
    MaximoConnector,
    _maximo_criticality,
    _parse_asset,
    _parse_datetime,
    _parse_maintenance_plan,
    _parse_spare_part,
    _parse_work_order,
    _require_httpx,
    _reverse_priority,
    _reverse_worktype,
)
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.maintenance_plan import MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)
from machina.exceptions import ConnectorError

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


class TestParseAsset:
    def test_basic_fields(self) -> None:
        raw = {
            "assetnum": "PUMP-201",
            "description": "Centrifugal Pump",
            "location": "AREA-A",
            "manufacturer": "Grundfos",
            "modelnum": "CR 32-4",
            "serialnum": "SN-999",
            "priority": 1,
            "parent": "SYS-100",
        }
        asset = _parse_asset(raw)
        assert isinstance(asset, Asset)
        assert asset.id == "PUMP-201"
        assert asset.name == "Centrifugal Pump"
        assert asset.location == "AREA-A"
        assert asset.manufacturer == "Grundfos"
        assert asset.model == "CR 32-4"
        assert asset.serial_number == "SN-999"
        assert asset.criticality == Criticality.A
        assert asset.parent == "SYS-100"

    def test_criticality_mapping(self) -> None:
        assert _parse_asset({"assetnum": "T1", "priority": 1}).criticality == Criticality.A
        assert _parse_asset({"assetnum": "T2", "priority": 2}).criticality == Criticality.B
        assert _parse_asset({"assetnum": "T3", "priority": 3}).criticality == Criticality.C
        assert _parse_asset({"assetnum": "T4", "priority": 99}).criticality == Criticality.C

    def test_criticality_non_numeric_defaults_to_c(self) -> None:
        """Non-castable priority must fallback to C, not crash."""
        assert _maximo_criticality("high") == Criticality.C
        assert _maximo_criticality(None) == Criticality.C

    def test_missing_fields_tolerated(self) -> None:
        asset = _parse_asset({"assetnum": "X"})
        assert asset.id == "X"
        assert asset.name == ""

    def test_extra_fields_in_metadata(self) -> None:
        raw = {"assetnum": "A", "description": "B", "siteid": "SITE1"}
        asset = _parse_asset(raw)
        assert "siteid" in asset.metadata

    def test_asset_type_map_applies_classstructureid(self) -> None:
        """With a map, classstructureid drives AssetType classification."""
        type_map = {
            "VESSELS": AssetType.STATIC_EQUIPMENT,
            "INSTRUMENTS": AssetType.INSTRUMENT,
        }
        vessel = _parse_asset({"assetnum": "V1", "classstructureid": "VESSELS"}, type_map)
        instrument = _parse_asset({"assetnum": "I1", "classstructureid": "INSTRUMENTS"}, type_map)
        assert vessel.type == AssetType.STATIC_EQUIPMENT
        assert instrument.type == AssetType.INSTRUMENT

    def test_asset_type_map_falls_back_to_rotating(self) -> None:
        """Unmapped classstructureid falls back to ROTATING_EQUIPMENT."""
        type_map = {"VESSELS": AssetType.STATIC_EQUIPMENT}
        unknown = _parse_asset({"assetnum": "X", "classstructureid": "MISC"}, type_map)
        assert unknown.type == AssetType.ROTATING_EQUIPMENT

    def test_asset_type_map_uses_assettype_fallback(self) -> None:
        """When classstructureid is missing, assettype is consulted."""
        type_map = {"ELECTRICAL_GEAR": AssetType.ELECTRICAL}
        asset = _parse_asset({"assetnum": "E1", "assettype": "ELECTRICAL_GEAR"}, type_map)
        assert asset.type == AssetType.ELECTRICAL

    def test_no_type_map_defaults_to_rotating(self) -> None:
        """Without a map the legacy behaviour holds (hardcoded rotating)."""
        asset = _parse_asset({"assetnum": "P1", "classstructureid": "PUMPS"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT


class TestParseWorkOrder:
    def test_basic_corrective(self) -> None:
        raw = {
            "wonum": "WO-001",
            "description": "Fix leak",
            "wopriority": 2,
            "status": "INPRG",
            "worktype": "CM",
            "assetnum": "PUMP-201",
            "reportdate": "2025-06-01T10:00:00Z",
            "changedate": "2025-06-02T08:00:00Z",
        }
        wo = _parse_work_order(raw)
        assert isinstance(wo, WorkOrder)
        assert wo.id == "WO-001"
        assert wo.priority == Priority.HIGH
        assert wo.status == WorkOrderStatus.IN_PROGRESS
        assert wo.type == WorkOrderType.CORRECTIVE

    def test_preventive(self) -> None:
        raw = {
            "wonum": "WO-002",
            "worktype": "PM",
            "wopriority": 3,
            "status": "WAPPR",
            "assetnum": "PUMP-202",
        }
        wo = _parse_work_order(raw)
        assert wo.type == WorkOrderType.PREVENTIVE
        assert wo.status == WorkOrderStatus.CREATED

    def test_unknown_status_defaults_to_created(self) -> None:
        raw = {"wonum": "WO-003", "status": "XYZZY", "assetnum": "A"}
        wo = _parse_work_order(raw)
        assert wo.status == WorkOrderStatus.CREATED

    def test_non_numeric_priority_defaults_to_medium(self) -> None:
        """A non-castable wopriority must not crash; defaults to MEDIUM."""
        raw = {"wonum": "WO-004", "wopriority": "urgent", "assetnum": "A"}
        wo = _parse_work_order(raw)
        assert wo.priority == Priority.MEDIUM


class TestParseSparePart:
    def test_basic(self) -> None:
        raw = {
            "itemnum": "BRG-6205",
            "description": "Bearing SKF 6205",
            "curbal": 50,
            "reorder": 10,
            "avgcost": 35.0,
            "location": "WHSE-1",
        }
        sp = _parse_spare_part(raw)
        assert isinstance(sp, SparePart)
        assert sp.sku == "BRG-6205"
        assert sp.stock_quantity == 50
        assert sp.reorder_point == 10
        assert sp.unit_cost == 35.0

    def test_extra_fields_in_metadata(self) -> None:
        """Maximo-specific inventory columns must land in metadata."""
        raw = {
            "itemnum": "BRG-6205",
            "description": "Bearing",
            "curbal": 1,
            "siteid": "BEDFORD",
            "binnum": "B-42",
        }
        sp = _parse_spare_part(raw)
        assert sp.metadata["siteid"] == "BEDFORD"
        assert sp.metadata["binnum"] == "B-42"
        # Known fields must NOT leak into metadata.
        assert "itemnum" not in sp.metadata


class TestParseDatetime:
    """Verify Maximo ISO-8601 timestamp parsing."""

    def test_zulu_suffix(self) -> None:
        dt = _parse_datetime("2025-06-01T10:00:00Z")
        assert dt.year == 2025
        assert dt.tzinfo is not None

    def test_naive_iso_defaults_to_utc(self) -> None:
        dt = _parse_datetime("2025-06-01T10:00:00")
        assert dt.tzinfo is not None
        assert dt.year == 2025


class TestParseMaintenancePlan:
    def test_active_plan(self) -> None:
        raw = {
            "pmnum": "PM-001",
            "description": "Monthly inspection",
            "assetnum": "PUMP-201",
            "frequency": 30,
            "status": "ACTIVE",
        }
        plan = _parse_maintenance_plan(raw)
        assert isinstance(plan, MaintenancePlan)
        assert plan.id == "PM-001"
        assert plan.interval.days == 30
        assert plan.active is True

    def test_inactive_plan(self) -> None:
        raw = {"pmnum": "PM-002", "status": "INACTIVE", "assetnum": "A"}
        plan = _parse_maintenance_plan(raw)
        assert plan.active is False


class TestReverseMapping:
    def test_reverse_priority(self) -> None:
        assert _reverse_priority(Priority.EMERGENCY) == 1
        assert _reverse_priority(Priority.HIGH) == 2
        assert _reverse_priority(Priority.MEDIUM) == 3
        assert _reverse_priority(Priority.LOW) == 4

    def test_reverse_worktype(self) -> None:
        assert _reverse_worktype(WorkOrderType.CORRECTIVE) == "CM"
        assert _reverse_worktype(WorkOrderType.PREVENTIVE) == "PM"
        assert _reverse_worktype(WorkOrderType.PREDICTIVE) == "CP"
        assert _reverse_worktype(WorkOrderType.IMPROVEMENT) == "EV"


# ---------------------------------------------------------------------------
# Connector lifecycle (no HTTP)
# ---------------------------------------------------------------------------


class TestConnectorLifecycle:
    def _make(self) -> MaximoConnector:
        return MaximoConnector(
            url="https://maximo.example.com",
            auth=ApiKeyHeaderAuth(header_name="apikey", value="test"),
        )

    def test_capabilities(self) -> None:
        conn = self._make()
        assert "read_assets" in conn.capabilities
        assert "create_work_order" in conn.capabilities
        assert "read_maintenance_plans" in conn.capabilities

    @pytest.mark.asyncio
    async def test_read_before_connect_raises(self) -> None:
        conn = self._make()
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_assets()

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_when_disconnected(self) -> None:
        conn = self._make()
        health = await conn.health_check()
        assert health.status.value == "unhealthy"


class TestRequireHttpx:
    def test_import_error_gives_actionable_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "httpx", None)
        with pytest.raises(ConnectorError, match="pip install machina-ai"):
            _require_httpx()
