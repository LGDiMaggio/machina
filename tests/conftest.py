"""Shared test fixtures for Machina."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import pytest
import structlog

from machina.connectors.base import set_sandbox_mode
from machina.domain.alarm import Alarm, Severity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO

    from structlog.typing import ExcInfo
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import FailureImpact, Priority, WorkOrder, WorkOrderType


@pytest.fixture(autouse=True)
def _reset_sandbox_mode() -> Iterator[None]:
    """Reset the global sandbox contextvar to ``False`` around every test.

    The ``Agent`` constructor and ``Agent.sandbox`` setter call
    ``set_sandbox_mode`` so that the connector-level ``@sandbox_aware``
    decorator sees the same value as the workflow engine.  That is the
    correct production behaviour but it leaks across tests, because
    ``contextvars`` are process-wide unless run in an isolated Context.
    This fixture pins the contextvar to ``False`` before each test and
    restores it after, so tests that assert on the default state stay
    deterministic regardless of execution order.
    """
    set_sandbox_mode(False)
    try:
        yield
    finally:
        set_sandbox_mode(False)


def _print_frame_locals(sio: TextIO, exc_info: ExcInfo) -> None:
    """Write every traceback frame's local variables, as rich's ``show_locals`` does."""
    tb = exc_info[2]
    while tb is not None:
        sio.write(f"\n{tb.tb_frame.f_code.co_name} locals: {tb.tb_frame.f_locals!r}")
        tb = tb.tb_next


@pytest.fixture
def frame_locals_renderer() -> structlog.dev.ConsoleRenderer:
    """A console renderer that prints frame locals for any ``exc_info`` it receives.

    It stands in for structlog's default renderer with ``rich`` installed
    (``RichTracebackFormatter(show_locals=True)``), so a leak test fails
    whether or not ``rich`` is in the environment.
    """
    return structlog.dev.ConsoleRenderer(colors=False, exception_formatter=_print_frame_locals)


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
