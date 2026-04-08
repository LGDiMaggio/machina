"""Shared test fixtures for Machina."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from machina.domain.alarm import Alarm, Severity
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import FailureImpact, Priority, WorkOrder, WorkOrderType


@pytest.fixture
def sample_asset() -> Asset:
    """A representative rotating-equipment asset."""
    return Asset(
        id="P-201",
        name="Cooling Water Pump",
        type=AssetType.ROTATING_EQUIPMENT,
        location="Building A / Line 2 / Cooling System",
        manufacturer="Grundfos",
        model="CR 32-2",
        serial_number="GF-2019-44521",
        install_date=date(2019, 6, 15),
        criticality=Criticality.A,
        parent="COOLING-SYS-01",
        equipment_class_code="PU",
    )


@pytest.fixture
def sample_work_order() -> WorkOrder:
    """A corrective work order for bearing repair."""
    return WorkOrder(
        id="WO-2026-1842",
        type=WorkOrderType.CORRECTIVE,
        priority=Priority.HIGH,
        asset_id="P-201",
        description="Excessive vibration detected on bearing DE side",
        failure_mode="BEARING_WEAR",
        failure_impact=FailureImpact.CRITICAL,
        failure_cause="Expected wear and tear",
    )


@pytest.fixture
def sample_failure_mode() -> FailureMode:
    """Bearing wear failure mode with detection info."""
    return FailureMode(
        code="BEAR-WEAR-01",
        name="Bearing Wear — Drive End",
        mechanism="fatigue",
        category="mechanical",
        detection_methods=["vibration_analysis", "temperature_monitoring"],
        typical_indicators=["increased_vibration", "elevated_temperature", "noise"],
        recommended_actions=["replace_bearing", "check_alignment", "verify_lubrication"],
        mtbf_hours=26000,
        iso_14224_code="VIB",
    )


@pytest.fixture
def sample_spare_part() -> SparePart:
    """A bearing spare part with inventory data."""
    return SparePart(
        sku="SKF-6310",
        name="Deep Groove Ball Bearing 6310",
        manufacturer="SKF",
        compatible_assets=["P-201", "P-202", "P-305"],
        stock_quantity=4,
        reorder_point=2,
        lead_time_days=5,
        unit_cost=45.00,
        warehouse_location="W1-R3-S12",
    )


@pytest.fixture
def sample_alarm() -> Alarm:
    """A vibration alarm on pump P-201."""
    return Alarm(
        id="ALM-2026-04-06-0847",
        asset_id="P-201",
        severity=Severity.WARNING,
        parameter="vibration_velocity_mm_s",
        value=7.8,
        threshold=6.0,
        unit="mm/s",
        timestamp=datetime(2026, 4, 6, 8, 47, 23),
        source="opcua://plc-line2/pump-p201/vib-de",
    )


@pytest.fixture
def sample_maintenance_plan() -> MaintenancePlan:
    """Quarterly pump inspection plan."""
    return MaintenancePlan(
        id="MP-P201-QUARTERLY",
        asset_id="P-201",
        name="Quarterly Pump Inspection",
        interval=Interval(months=3),
        tasks=[
            "Visual inspection of seals and gaskets",
            "Vibration measurement at bearing points",
            "Lubrication check and top-up",
            "Alignment verification",
        ],
        estimated_duration_hours=2,
        required_skills=["mechanical"],
    )
