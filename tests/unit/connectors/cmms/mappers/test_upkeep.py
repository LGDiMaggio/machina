"""Unit tests for the UpKeep mapper — pure ``dict`` → Entity conversions."""

from __future__ import annotations

from datetime import UTC, datetime

from machina.connectors.cmms.mappers.upkeep import (
    parse_asset,
    parse_datetime,
    parse_spare_part,
    parse_work_order,
    reverse_priority,
    reverse_status,
)
from machina.domain.asset import AssetType, Criticality
from machina.domain.work_order import (
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)


class TestParseAssetPublicAPI:
    def test_happy_path(self) -> None:
        asset = parse_asset({"id": "1", "name": "Pump"})
        assert asset.id == "1"
        assert asset.name == "Pump"
        assert asset.criticality == Criticality.C  # UpKeep has no criticality field

    def test_category_rotating_equipment(self) -> None:
        asset = parse_asset({"id": "2", "category": "Rotating Equipment"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_unknown_category_falls_back_to_rotating(self) -> None:
        asset = parse_asset({"id": "3", "category": "Mystery"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_empty_dict_raises_validation_error(self) -> None:
        """Empty input has no id → Asset pydantic validator rejects."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id cannot be empty"):
            parse_asset({})


class TestParseWorkOrderPublicAPI:
    def test_priority_0_is_low(self) -> None:
        """UpKeep uses 0-indexed priority (0 = lowest)."""
        wo = parse_work_order({"id": "1", "priority": 0})
        assert wo.priority == Priority.LOW

    def test_priority_3_is_emergency(self) -> None:
        wo = parse_work_order({"id": "2", "priority": 3})
        assert wo.priority == Priority.EMERGENCY

    def test_category_preventive_maps_to_preventive_type(self) -> None:
        wo = parse_work_order({"id": "3", "category": "preventive"})
        assert wo.type == WorkOrderType.PREVENTIVE

    def test_default_category_maps_to_corrective(self) -> None:
        wo = parse_work_order({"id": "4"})
        assert wo.type == WorkOrderType.CORRECTIVE

    def test_status_on_hold_maps_to_assigned(self) -> None:
        wo = parse_work_order({"id": "5", "status": "on hold"})
        assert wo.status == WorkOrderStatus.ASSIGNED


class TestParseSparePartSku:
    """SKU preference order: partNumber > barcode > id."""

    def test_prefers_part_number(self) -> None:
        sp = parse_spare_part({"id": "internal", "partNumber": "PN-1", "barcode": "BC-1"})
        assert sp.sku == "PN-1"

    def test_falls_back_to_barcode(self) -> None:
        sp = parse_spare_part({"id": "internal", "barcode": "BC-1"})
        assert sp.sku == "BC-1"

    def test_falls_back_to_id_when_neither_provided(self) -> None:
        sp = parse_spare_part({"id": "internal"})
        assert sp.sku == "internal"


class TestParseDatetime:
    def test_iso_with_z(self) -> None:
        dt = parse_datetime("2024-06-01T09:30:00Z")
        assert dt == datetime(2024, 6, 1, 9, 30, 0, tzinfo=UTC)


class TestReverseMaps:
    def test_reverse_priority_low_is_0(self) -> None:
        """Inverse of 0-indexed priority: LOW → 0."""
        assert reverse_priority(Priority.LOW) == 0

    def test_reverse_priority_emergency_is_3(self) -> None:
        assert reverse_priority(Priority.EMERGENCY) == 3

    def test_reverse_status_closed_maps_to_complete(self) -> None:
        """UpKeep has no distinct CLOSED state — both COMPLETED and CLOSED → 'complete'."""
        assert reverse_status(WorkOrderStatus.COMPLETED) == "complete"
        assert reverse_status(WorkOrderStatus.CLOSED) == "complete"

    def test_reverse_status_cancelled_maps_to_on_hold(self) -> None:
        """UpKeep has no distinct CANCELLED state — maps to 'on hold'."""
        assert reverse_status(WorkOrderStatus.CANCELLED) == "on hold"
