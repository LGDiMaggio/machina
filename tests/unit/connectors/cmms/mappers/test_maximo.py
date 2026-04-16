"""Unit tests for the Maximo mapper — pure ``dict`` → Entity conversions."""

from __future__ import annotations

from datetime import UTC, datetime

from machina.connectors.cmms.mappers.maximo import (
    parse_asset,
    parse_datetime,
    parse_work_order,
    resolve_asset_type,
    reverse_priority,
    reverse_status,
    reverse_worktype,
)
from machina.domain.asset import AssetType, Criticality
from machina.domain.work_order import (
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)


class TestParseAssetPublicAPI:
    def test_happy_path(self) -> None:
        asset = parse_asset({"assetnum": "A-1", "description": "Pump"})
        assert asset.id == "A-1"
        assert asset.name == "Pump"
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_empty_dict_raises_validation_error(self) -> None:
        """Empty input has no assetnum → Asset pydantic validator rejects."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id cannot be empty"):
            parse_asset({})

    def test_asset_type_map_honoured(self) -> None:
        type_map = {"PUMPS": AssetType.ROTATING_EQUIPMENT, "VESSELS": AssetType.STATIC_EQUIPMENT}
        asset = parse_asset({"assetnum": "V-1", "classstructureid": "VESSELS"}, type_map)
        assert asset.type == AssetType.STATIC_EQUIPMENT

    def test_asset_type_map_fallback_to_rotating(self) -> None:
        type_map = {"PUMPS": AssetType.ROTATING_EQUIPMENT}
        asset = parse_asset({"assetnum": "X-1", "classstructureid": "UNKNOWN"}, type_map)
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_priority_1_is_critical(self) -> None:
        asset = parse_asset({"assetnum": "A-2", "priority": 1})
        assert asset.criticality == Criticality.A


class TestParseWorkOrderPublicAPI:
    def test_status_inprg_maps_to_in_progress(self) -> None:
        wo = parse_work_order({"wonum": "W-1", "status": "INPRG"})
        assert wo.status == WorkOrderStatus.IN_PROGRESS

    def test_priority_non_numeric_falls_back_to_medium(self) -> None:
        """Non-numeric ``wopriority`` must not raise — fallback to MEDIUM."""
        wo = parse_work_order({"wonum": "W-2", "wopriority": "garbage"})
        assert wo.priority == Priority.MEDIUM

    def test_missing_priority_defaults_to_medium(self) -> None:
        wo = parse_work_order({"wonum": "W-3"})
        assert wo.priority == Priority.MEDIUM

    def test_worktype_pm_maps_to_preventive(self) -> None:
        wo = parse_work_order({"wonum": "W-4", "worktype": "PM"})
        assert wo.type == WorkOrderType.PREVENTIVE


class TestResolveAssetType:
    def test_none_map_returns_rotating(self) -> None:
        assert (
            resolve_asset_type({"classstructureid": "anything"}, None)
            == AssetType.ROTATING_EQUIPMENT
        )

    def test_empty_map_returns_rotating(self) -> None:
        assert (
            resolve_asset_type({"classstructureid": "anything"}, {})
            == AssetType.ROTATING_EQUIPMENT
        )

    def test_assettype_used_when_classstructureid_missing(self) -> None:
        m = {"INSTR": AssetType.INSTRUMENT}
        assert resolve_asset_type({"assettype": "INSTR"}, m) == AssetType.INSTRUMENT


class TestParseDatetime:
    def test_iso_with_z(self) -> None:
        dt = parse_datetime("2024-01-01T12:00:00Z")
        assert dt == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_iso_naive_gets_utc_tzinfo(self) -> None:
        dt = parse_datetime("2024-01-01T12:00:00")
        assert dt.tzinfo == UTC


class TestReverseMaps:
    def test_reverse_priority_emergency_is_1(self) -> None:
        assert reverse_priority(Priority.EMERGENCY) == 1

    def test_reverse_priority_low_is_4(self) -> None:
        assert reverse_priority(Priority.LOW) == 4

    def test_reverse_worktype_predictive_is_cp(self) -> None:
        assert reverse_worktype(WorkOrderType.PREDICTIVE) == "CP"

    def test_reverse_status_cancelled_is_can(self) -> None:
        assert reverse_status(WorkOrderStatus.CANCELLED) == "CAN"
