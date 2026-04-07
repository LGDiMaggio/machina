"""Tests for the machina package public API exports."""

from __future__ import annotations

import machina


class TestPublicAPI:
    """Verify that the public API exports are correct and accessible."""

    def test_version_is_set(self) -> None:
        assert machina.__version__ == "0.1.0"

    def test_all_domain_entities_exported(self) -> None:
        expected_names = [
            "Alarm",
            "Asset",
            "AssetType",
            "Criticality",
            "FailureMode",
            "Interval",
            "MaintenancePlan",
            "Plant",
            "Priority",
            "Severity",
            "SparePart",
            "WorkOrder",
            "WorkOrderStatus",
            "WorkOrderType",
        ]
        for name in expected_names:
            assert hasattr(machina, name), f"{name} not exported from machina"

    def test_all_list_matches_exports(self) -> None:
        for name in machina.__all__:
            assert hasattr(machina, name), f"{name} in __all__ but not importable"

    def test_domain_entities_are_correct_types(self) -> None:
        from machina.domain.alarm import Alarm, Severity
        from machina.domain.asset import Asset, AssetType, Criticality
        from machina.domain.failure_mode import FailureMode
        from machina.domain.maintenance_plan import Interval, MaintenancePlan
        from machina.domain.plant import Plant
        from machina.domain.spare_part import SparePart
        from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType

        assert machina.Alarm is Alarm
        assert machina.Asset is Asset
        assert machina.AssetType is AssetType
        assert machina.Criticality is Criticality
        assert machina.FailureMode is FailureMode
        assert machina.Interval is Interval
        assert machina.MaintenancePlan is MaintenancePlan
        assert machina.Plant is Plant
        assert machina.Priority is Priority
        assert machina.Severity is Severity
        assert machina.SparePart is SparePart
        assert machina.WorkOrder is WorkOrder
        assert machina.WorkOrderStatus is WorkOrderStatus
        assert machina.WorkOrderType is WorkOrderType
