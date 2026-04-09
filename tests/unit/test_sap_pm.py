"""Unit tests for SapPmConnector.

Covers entity parsing helpers, SAP-specific date formats, status mapping,
and basic lifecycle without network calls.
"""

from __future__ import annotations

import pytest

from machina.connectors.cmms.auth import BasicAuth, OAuth2ClientCredentials
from machina.connectors.cmms.sap_pm import (
    SapPmConnector,
    _map_sap_status,
    _parse_asset,
    _parse_maintenance_plan,
    _parse_sap_datetime,
    _parse_spare_part,
    _parse_work_order,
    _reverse_order_type,
    _reverse_priority,
    _sap_criticality,
    _sap_cycle_to_interval,
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
            "Equipment": "10000001",
            "EquipmentName": "Centrifugal Pump P-201",
            "EquipmentCategory": "M",
            "FunctionalLocation": "PLANT-A-AREA1",
            "Manufacturer": "Grundfos",
            "ModelNumber": "CR 32-4",
            "SerialNumber": "SN-12345",
            "ABCIndicator": "A",
            "SuperordinateEquipment": "20000001",
            "EquipmentClassCode": "PU",
        }
        asset = _parse_asset(raw)
        assert isinstance(asset, Asset)
        assert asset.id == "10000001"
        assert asset.name == "Centrifugal Pump P-201"
        assert asset.type == AssetType.ROTATING_EQUIPMENT
        assert asset.location == "PLANT-A-AREA1"
        assert asset.manufacturer == "Grundfos"
        assert asset.criticality == Criticality.A
        assert asset.parent == "20000001"
        assert asset.equipment_class_code == "PU"

    def test_electrical_category(self) -> None:
        raw = {"Equipment": "E1", "EquipmentCategory": "E"}
        assert _parse_asset(raw).type == AssetType.ELECTRICAL

    def test_unknown_category_defaults(self) -> None:
        raw = {"Equipment": "X1", "EquipmentCategory": "Z"}
        assert _parse_asset(raw).type == AssetType.ROTATING_EQUIPMENT

    def test_missing_fields_tolerated(self) -> None:
        raw = {"Equipment": "X2"}
        asset = _parse_asset(raw)
        assert asset.id == "X2"
        assert asset.name == ""


class TestSapCriticality:
    def test_abc_mapping(self) -> None:
        assert _sap_criticality("A") == Criticality.A
        assert _sap_criticality("B") == Criticality.B
        assert _sap_criticality("C") == Criticality.C
        assert _sap_criticality("") == Criticality.C
        assert _sap_criticality("a") == Criticality.A  # case insensitive


class TestParseWorkOrder:
    def test_corrective_order(self) -> None:
        raw = {
            "MaintenanceOrder": "4000001",
            "MaintenanceOrderDesc": "Fix leaking pump",
            "MaintenanceOrderType": "PM01",
            "MaintPriority": "2",
            "MaintenanceOrderSystemStatus": "REL",
            "Equipment": "10000001",
            "CreationDate": "2025-06-01T10:00:00Z",
            "LastChangeDateTime": "2025-06-02T08:00:00Z",
        }
        wo = _parse_work_order(raw)
        assert isinstance(wo, WorkOrder)
        assert wo.id == "4000001"
        assert wo.type == WorkOrderType.CORRECTIVE
        assert wo.priority == Priority.HIGH
        assert wo.status == WorkOrderStatus.ASSIGNED

    def test_preventive_order(self) -> None:
        raw = {
            "MaintenanceOrder": "4000002",
            "MaintenanceOrderType": "PM02",
            "MaintPriority": "3",
            "MaintenanceOrderSystemStatus": "CRTD",
            "Equipment": "E1",
        }
        wo = _parse_work_order(raw)
        assert wo.type == WorkOrderType.PREVENTIVE
        assert wo.status == WorkOrderStatus.CREATED


class TestMapSapStatus:
    def test_compound_status(self) -> None:
        assert _map_sap_status("CRTD REL MANC") == WorkOrderStatus.ASSIGNED
        assert _map_sap_status("CRTD REL PCNF") == WorkOrderStatus.IN_PROGRESS
        assert _map_sap_status("TECO CNF") == WorkOrderStatus.CLOSED

    def test_simple_status(self) -> None:
        assert _map_sap_status("CRTD") == WorkOrderStatus.CREATED
        assert _map_sap_status("DLFL") == WorkOrderStatus.CANCELLED

    def test_unknown_defaults_to_created(self) -> None:
        assert _map_sap_status("XYZZY") == WorkOrderStatus.CREATED


class TestParseSapDatetime:
    def test_iso_format(self) -> None:
        dt = _parse_sap_datetime("2025-06-01T10:00:00Z")
        assert dt.year == 2025
        assert dt.month == 6

    def test_sap_date_format(self) -> None:
        dt = _parse_sap_datetime("/Date(1717228800000)/")
        assert dt.year == 2024

    def test_sap_date_with_offset(self) -> None:
        dt = _parse_sap_datetime("/Date(1717228800000+0000)/")
        assert dt.year == 2024

    def test_plain_date(self) -> None:
        dt = _parse_sap_datetime("2025-06-01")
        assert dt.year == 2025

    def test_compact_date(self) -> None:
        dt = _parse_sap_datetime("20250601")
        assert dt.year == 2025

    def test_empty_string_returns_now(self) -> None:
        dt = _parse_sap_datetime("")
        assert dt.tzinfo is not None


class TestParseSparePart:
    def test_basic(self) -> None:
        raw = {
            "Material": "MAT-001",
            "MaterialDescription": "Bearing SKF 6205",
            "AvailableQuantity": 50,
            "StandardPrice": 35.0,
            "StorageLocation": "SL01",
        }
        sp = _parse_spare_part(raw)
        assert isinstance(sp, SparePart)
        assert sp.sku == "MAT-001"
        assert sp.stock_quantity == 50


class TestParseMaintenancePlan:
    def test_daily_interval(self) -> None:
        raw = {
            "MaintenancePlan": "MP-001",
            "MaintenancePlanDesc": "Daily inspection",
            "Equipment": "E1",
            "MaintenancePlanCycleValue": 1,
            "MaintenancePlanCycleUnit": "DAY",
            "MaintenancePlanStatus": "ACTV",
        }
        plan = _parse_maintenance_plan(raw)
        assert isinstance(plan, MaintenancePlan)
        assert plan.interval.days == 1
        assert plan.active is True

    def test_weekly_interval(self) -> None:
        raw = {
            "MaintenancePlan": "MP-002",
            "MaintenancePlanCycleValue": 2,
            "MaintenancePlanCycleUnit": "WK",
            "Equipment": "E1",
        }
        plan = _parse_maintenance_plan(raw)
        assert plan.interval.weeks == 2

    def test_monthly_interval(self) -> None:
        raw = {
            "MaintenancePlan": "MP-003",
            "MaintenancePlanCycleValue": 3,
            "MaintenancePlanCycleUnit": "MON",
            "Equipment": "E1",
        }
        plan = _parse_maintenance_plan(raw)
        assert plan.interval.months == 3

    def test_hours_interval(self) -> None:
        raw = {
            "MaintenancePlan": "MP-004",
            "MaintenancePlanCycleValue": 500,
            "MaintenancePlanCycleUnit": "H",
            "Equipment": "E1",
        }
        plan = _parse_maintenance_plan(raw)
        assert plan.interval.hours == 500


class TestSapCycleToInterval:
    def test_dag_variant(self) -> None:
        i = _sap_cycle_to_interval(30, "TAG")
        assert i.days == 30

    def test_woc_variant(self) -> None:
        i = _sap_cycle_to_interval(4, "WOC")
        assert i.weeks == 4

    def test_std_variant(self) -> None:
        i = _sap_cycle_to_interval(1000, "STD")
        assert i.hours == 1000


class TestReverseMapping:
    def test_reverse_priority(self) -> None:
        assert _reverse_priority(Priority.EMERGENCY) == "1"
        assert _reverse_priority(Priority.HIGH) == "2"
        assert _reverse_priority(Priority.MEDIUM) == "3"
        assert _reverse_priority(Priority.LOW) == "4"

    def test_reverse_order_type(self) -> None:
        assert _reverse_order_type(WorkOrderType.CORRECTIVE) == "PM01"
        assert _reverse_order_type(WorkOrderType.PREVENTIVE) == "PM02"
        assert _reverse_order_type(WorkOrderType.PREDICTIVE) == "PM03"
        assert _reverse_order_type(WorkOrderType.IMPROVEMENT) == "PM04"


# ---------------------------------------------------------------------------
# Connector lifecycle (no HTTP)
# ---------------------------------------------------------------------------


class TestConnectorLifecycle:
    def _make(self) -> SapPmConnector:
        return SapPmConnector(
            url="https://sap.example.com/sap/opu/odata/sap",
            auth=BasicAuth(username="sapuser", password="secret"),
            sap_client="100",
        )

    def test_capabilities(self) -> None:
        conn = self._make()
        assert "read_assets" in conn.capabilities
        assert "create_work_order" in conn.capabilities
        assert "read_maintenance_plans" in conn.capabilities

    def test_sap_client_stored(self) -> None:
        conn = self._make()
        assert conn._sap_client == "100"  # noqa: SLF001

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
