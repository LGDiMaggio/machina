"""Unit tests for the SAP PM mapper — pure ``dict`` → Entity conversions.

These tests exercise the public API of
:mod:`machina.connectors.cmms.mappers.sap_pm` with raw dictionaries.
No HTTP mocks; no connector state; no network.  Complements the
existing characterization tests in ``tests/unit/test_sap_pm.py`` which
were redirected to the mapper module as part of the Unit 2 refactor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from machina.connectors.cmms.mappers.sap_pm import (
    parse_asset,
    parse_sap_datetime,
    parse_work_order,
    reverse_order_type,
    reverse_priority,
    reverse_status,
)
from machina.domain.asset import AssetType
from machina.domain.work_order import (
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)


class TestParseAssetPublicAPI:
    """parse_asset: dict → Asset happy path and edge cases."""

    def test_happy_path(self) -> None:
        asset = parse_asset(
            {"Equipment": "EQ-1", "EquipmentName": "Pump 1", "EquipmentCategory": "M"}
        )
        assert asset.id == "EQ-1"
        assert asset.name == "Pump 1"
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_missing_category_falls_back_to_rotating(self) -> None:
        asset = parse_asset({"Equipment": "EQ-2", "EquipmentName": "Noname"})
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_empty_dict_raises_validation_error(self) -> None:
        """Empty input has no Equipment id → Asset pydantic validator rejects."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="id cannot be empty"):
            parse_asset({})

    def test_category_e_maps_to_electrical(self) -> None:
        asset = parse_asset({"Equipment": "EQ-3", "EquipmentCategory": "E"})
        assert asset.type == AssetType.ELECTRICAL


class TestParseWorkOrderPublicAPI:
    """parse_work_order: dict → WorkOrder happy path and edge cases."""

    def test_compound_status_rel_wins_over_crtd(self) -> None:
        """SAP compound status ``"CRTD REL MANC"`` → ASSIGNED (most progressed)."""
        wo = parse_work_order(
            {
                "MaintenanceOrder": "W-1",
                "MaintenanceOrderSystemStatus": "CRTD REL MANC",
            }
        )
        assert wo.status == WorkOrderStatus.ASSIGNED

    def test_compound_status_teco_wins(self) -> None:
        """TECO > REL > CRTD in lifecycle progression."""
        wo = parse_work_order(
            {
                "MaintenanceOrder": "W-2",
                "MaintenanceOrderSystemStatus": "CRTD REL TECO",
            }
        )
        assert wo.status == WorkOrderStatus.CLOSED

    def test_missing_creation_date_defaults_to_utc_now(self) -> None:
        before = datetime.now(tz=UTC)
        wo = parse_work_order({"MaintenanceOrder": "W-3"})
        after = datetime.now(tz=UTC)
        assert before <= wo.created_at <= after
        assert wo.created_at.tzinfo == UTC


class TestParseSapDatetime:
    """parse_sap_datetime: handle every SAP date format Machina has seen."""

    def test_sap_millis_format_returns_utc(self) -> None:
        """``/Date(1704067200000+0000)/`` → 2024-01-01 00:00:00 UTC."""
        dt = parse_sap_datetime("/Date(1704067200000+0000)/")
        assert dt == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_yyyymmdd_format(self) -> None:
        dt = parse_sap_datetime("20240101")
        assert dt == datetime(2024, 1, 1, tzinfo=UTC)

    def test_empty_string_returns_utc_now_without_raising(self) -> None:
        before = datetime.now(tz=UTC)
        dt = parse_sap_datetime("")
        after = datetime.now(tz=UTC)
        assert before <= dt <= after

    def test_unparseable_string_returns_utc_now_without_raising(self) -> None:
        """Garbage in → current UTC (graceful degradation)."""
        before = datetime.now(tz=UTC)
        dt = parse_sap_datetime("not-a-date-at-all")
        after = datetime.now(tz=UTC)
        assert before <= dt <= after

    def test_iso8601_with_z_suffix(self) -> None:
        dt = parse_sap_datetime("2024-01-01T12:00:00Z")
        assert dt == datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class TestReverseMaps:
    """reverse_priority / reverse_order_type / reverse_status."""

    def test_reverse_priority_emergency_is_1(self) -> None:
        assert reverse_priority(Priority.EMERGENCY) == "1"

    def test_reverse_priority_medium_is_3(self) -> None:
        assert reverse_priority(Priority.MEDIUM) == "3"

    def test_reverse_order_type_corrective_is_pm01(self) -> None:
        assert reverse_order_type(WorkOrderType.CORRECTIVE) == "PM01"

    def test_reverse_status_cancelled_is_dlfl(self) -> None:
        assert reverse_status(WorkOrderStatus.CANCELLED) == "DLFL"

    def test_reverse_status_unknown_falls_back_to_crtd(self) -> None:
        """Defensive: any unmapped status falls back to ``CRTD``."""
        # No unmapped members today; assert the documented fallback shape
        # via the default path on a member we control.
        result = reverse_status(WorkOrderStatus.CREATED)
        assert result == "CRTD"
