"""Unit tests for UpKeepConnector.

Covers entity parsing helpers, priority mappings, and basic lifecycle
without network calls. For HTTP-level integration tests see
``tests/integration/test_upkeep_rest.py``.
"""

from __future__ import annotations

import pytest

from machina.connectors.cmms.mappers.upkeep import (
    parse_asset as _parse_asset,
)
from machina.connectors.cmms.mappers.upkeep import (
    parse_datetime as _parse_datetime,
)
from machina.connectors.cmms.mappers.upkeep import (
    parse_maintenance_plans as _parse_maintenance_plans,
)
from machina.connectors.cmms.mappers.upkeep import (
    parse_spare_part as _parse_spare_part,
)
from machina.connectors.cmms.mappers.upkeep import (
    parse_work_order as _parse_work_order,
)
from machina.connectors.cmms.mappers.upkeep import (
    reverse_priority as _reverse_priority,
)
from machina.connectors.cmms.upkeep import UpKeepConnector, _require_httpx
from machina.domain.asset import Asset, AssetType
from machina.domain.maintenance_plan import Interval, MaintenancePlan
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
            "priority": 2,  # UpKeep: 2 = HIGH (0-indexed scale, 0-3)
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
            "priority": 0,  # UpKeep: 0 = LOW (lowest)
            "status": "complete",
            "assetId": "asset-2",
            "createdAt": "2025-05-01T08:00:00Z",
            "updatedAt": "2025-05-15T14:00:00Z",
        }
        wo = _parse_work_order(raw)
        assert wo.type == WorkOrderType.PREVENTIVE
        assert wo.priority == Priority.LOW
        assert wo.status == WorkOrderStatus.COMPLETED

    def test_emergency_priority(self) -> None:
        """UpKeep priority 3 (highest on the 0-3 scale) must map to EMERGENCY."""
        raw = {
            "id": "wo-e",
            "title": "Shutdown",
            "priority": 3,
            "status": "open",
            "assetId": "a",
        }
        wo = _parse_work_order(raw)
        assert wo.priority == Priority.EMERGENCY

    def test_in_progress_status_mapping(self) -> None:
        raw = {
            "id": "wo-3",
            "title": "Repair",
            "priority": 1,
            "status": "in progress",
            "assetId": "a",
        }
        wo = _parse_work_order(raw)
        assert wo.status == WorkOrderStatus.IN_PROGRESS


class TestParseSparePart:
    """Verify UpKeep part JSON → SparePart conversion."""

    def test_basic_spare_part_falls_back_to_id(self) -> None:
        """Without partNumber/barcode, the UpKeep record id is used as SKU."""
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

    def test_spare_part_prefers_part_number(self) -> None:
        """When partNumber is present it must win over the internal id."""
        raw = {
            "id": "part-1",
            "partNumber": "SKF-6205-2RS",
            "name": "Deep groove bearing",
            "quantity": 10,
        }
        sp = _parse_spare_part(raw)
        assert sp.sku == "SKF-6205-2RS"

    def test_spare_part_falls_back_to_barcode(self) -> None:
        """When only barcode is provided (no partNumber), barcode is the SKU."""
        raw = {
            "id": "part-2",
            "barcode": "1234567890",
            "name": "Bearing",
        }
        sp = _parse_spare_part(raw)
        assert sp.sku == "1234567890"

    def test_spare_part_preserves_extra_fields_in_metadata(self) -> None:
        """Unknown fields must be round-tripped via metadata, not dropped."""
        raw = {
            "id": "p9",
            "partNumber": "P-9",
            "name": "Gasket",
            "vendorId": "VND-77",
            "leadTimeDays": 14,
        }
        sp = _parse_spare_part(raw)
        assert sp.metadata["vendorId"] == "VND-77"
        assert sp.metadata["leadTimeDays"] == 14
        # Known fields must NOT leak into metadata.
        assert "id" not in sp.metadata
        assert "partNumber" not in sp.metadata


class TestParseMaintenancePlans:
    """Verify UpKeep PM template JSON → MaintenancePlan conversion."""

    def test_documented_template_maps_one_plan_per_schedule(self) -> None:
        # One template from "Get all PMs" (developers.onupkeep.com/#get-all-pms),
        # without the deletedAt/deletedBy markers of the documented example.
        raw = {
            "__v": 0,
            "_id": "6568e5bf7a771cd3d83b337c",
            "category": "Inspection",
            "createFirstWO": True,
            "createdAt": "2023-11-30T19:42:55.984Z",
            "createdBy": "N5lbFIv8LW",
            "estimatedTime": 100,
            "files": ["trfyrp53kw"],
            "images": [],
            "legacyPMId": "t69EvI5kMq",
            "mainDescription": "Oil change #FP-ZFP-1",
            "name": "Oil change #FP-ZFP-1",
            "note": "Oil change for asset #FP-ZFP-1",
            "partInventories": [],
            "priority": 3,
            "requiresSignature": False,
            "role": "JtdU6sKOwD",
            "schedules": [
                {
                    "asset": "BcmoxOY4mE",
                    "assignee": "Fu8PEA9P9G",
                    "bySetPosition": [],
                    "cadenceFreq": "DAILY",
                    "cadenceInterval": 3,
                    "cadenceType": "manual",
                    "endDate": "2024-10-09T08:42:24.000Z",
                    "excludedates": [],
                    "id": "65672e0c90176209662a4fc2",
                    "includeDates": [],
                    "isBasedOnCompletion": False,
                    "location": "c78MVjkbvh",
                    "monthdays": [],
                    "nextDueDate": "2023-12-11T08:42:24.000Z",
                    "nextTriggerDate": "2023-12-10T08:42:24.000Z",
                    "pmTemplate": "65672e0c90176209662a4fc1",
                    "repeatFrequency": "MONTHLY",
                    "repeatInterval": 7,
                    "role": "PUlegnw3ml",
                    "scheduleType": "EVERY_N_MONTHS",
                    "startDate": "2023-12-10T08:42:24.000Z",
                    "supportUsers": ["IDb8mbMV9l"],
                    "team": "SFejxVqERT",
                    "timeZone": "Asia/Kolkata",
                    "weekdays": [],
                },
                {
                    "asset": "BcmoxOY4mE",
                    "assignee": "Fu8PEA9P9G",
                    "bySetPosition": [],
                    "endDate": "2024-10-09T16:23:35.000Z",
                    "excludedates": [],
                    "id": "65672e0c90176209662a4fc4",
                    "includeDates": [],
                    "location": "c78MVjkbvh",
                    "meter": "vXMGBoLMJv",
                    "meterConditionValue": 10000,
                    "meterDueFrequency": "weeks",
                    "meterDueInterval": 1,
                    "monthdays": [],
                    "nextMeterReading": 45750,
                    "pmTemplate": "65672e0c90176209662a4fc1",
                    "role": "PUlegnw3ml",
                    "startDate": "2023-12-10T16:23:35.000Z",
                    "supportUsers": ["IDb8mbMV9l"],
                    "team": "SFejxVqERT",
                    "timeZone": "Asia/Kolkata",
                    "weekdays": [],
                },
            ],
            "tasks": [],
            "updatedAt": "2023-11-30T19:43:30.230Z",
        }
        plans = _parse_maintenance_plans(raw)
        assert len(plans) == 2
        calendar, meter = plans
        assert isinstance(calendar, MaintenancePlan)
        assert calendar.id == "65672e0c90176209662a4fc2"
        assert calendar.asset_id == "BcmoxOY4mE"
        assert calendar.name == "Oil change #FP-ZFP-1"
        assert calendar.interval == Interval(months=7)
        assert calendar.tasks == []
        assert calendar.estimated_duration_hours == 100
        assert calendar.active is True
        assert meter.id == "65672e0c90176209662a4fc4"
        assert meter.interval == Interval()


class TestParseDatetime:
    """Verify UpKeep ISO-8601 timestamp parsing."""

    def test_utc_zulu_suffix(self) -> None:
        dt = _parse_datetime("2025-06-01T10:00:00Z")
        assert dt.year == 2025
        assert dt.tzinfo is not None

    def test_naive_iso_defaults_to_utc(self) -> None:
        """A naive ISO string (no Z, no offset) must be coerced to UTC."""
        dt = _parse_datetime("2025-06-01T10:00:00")
        assert dt.tzinfo is not None
        assert dt.year == 2025


class TestReversePriority:
    """Verify Machina → UpKeep priority mapping (0-indexed, 0-3)."""

    def test_all_values(self) -> None:
        # Per UpKeep REST API v2: priority is an int where 0 = lowest, 3 = highest
        assert _reverse_priority(Priority.LOW) == 0
        assert _reverse_priority(Priority.MEDIUM) == 1
        assert _reverse_priority(Priority.HIGH) == 2
        assert _reverse_priority(Priority.EMERGENCY) == 3


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


class TestRequireHttpx:
    def test_import_error_gives_actionable_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "httpx", None)
        with pytest.raises(ConnectorError, match="pip install machina-ai"):
            _require_httpx()
