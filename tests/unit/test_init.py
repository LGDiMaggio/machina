"""Tests for the machina package public API exports."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import TYPE_CHECKING, NoReturn

import machina

if TYPE_CHECKING:
    import pytest


class TestPublicAPI:
    """Verify that the public API exports are correct and accessible."""

    def test_version_is_set(self) -> None:
        assert isinstance(machina.__version__, str)
        assert machina.__version__ != ""

    def test_version_matches_installed_metadata(self) -> None:
        assert machina.__version__ == importlib.metadata.version("machina-ai")

    def test_version_falls_back_when_package_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When distribution metadata is absent, __version__ uses the sentinel fallback."""

        def _raise_not_found(_name: str) -> NoReturn:
            raise importlib.metadata.PackageNotFoundError

        monkeypatch.setattr(importlib.metadata, "version", _raise_not_found)
        try:
            importlib.reload(machina)
            assert machina.__version__ == "0.0.0+unknown"
        finally:
            monkeypatch.undo()
            importlib.reload(machina)

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
