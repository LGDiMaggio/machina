"""Tests for domain services."""

from datetime import date

from machina.domain.alarm import Alarm, Severity
from machina.domain.failure_mode import FailureMode
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.services.failure_analyzer import FailureAnalyzer
from machina.domain.services.maintenance_scheduler import MaintenanceScheduler
from machina.domain.services.work_order_factory import WorkOrderFactory
from machina.domain.work_order import Priority, WorkOrderStatus, WorkOrderType


class TestFailureAnalyzer:
    """Test symptom-to-failure-mode matching."""

    def test_diagnose_matching_alarm(self) -> None:
        fm = FailureMode(
            code="BEAR-01",
            name="Bearing Wear",
            typical_indicators=["increased_vibration", "elevated_temperature"],
        )
        analyzer = FailureAnalyzer(failure_modes=[fm])
        alarm = Alarm(
            id="A1",
            asset_id="P-1",
            severity=Severity.WARNING,
            parameter="increased_vibration",
            value=8.0,
            threshold=6.0,
        )
        results = analyzer.diagnose([alarm])
        assert len(results) == 1
        assert results[0].code == "BEAR-01"

    def test_diagnose_no_match(self) -> None:
        fm = FailureMode(
            code="BEAR-01",
            name="Bearing Wear",
            typical_indicators=["increased_vibration"],
        )
        analyzer = FailureAnalyzer(failure_modes=[fm])
        alarm = Alarm(
            id="A1",
            asset_id="P-1",
            severity=Severity.INFO,
            parameter="pressure_drop",
            value=2.0,
            threshold=1.5,
        )
        results = analyzer.diagnose([alarm])
        assert len(results) == 0

    def test_register_failure_mode(self) -> None:
        analyzer = FailureAnalyzer()
        fm = FailureMode(code="X", name="X", typical_indicators=["noise"])
        analyzer.register_failure_mode(fm)
        alarm = Alarm(
            id="A1",
            asset_id="P-1",
            severity=Severity.WARNING,
            parameter="noise",
            value=90.0,
            threshold=85.0,
        )
        assert len(analyzer.diagnose([alarm])) == 1


class TestWorkOrderFactory:
    """Test work order creation."""

    def test_create_basic_work_order(self) -> None:
        factory = WorkOrderFactory()
        wo = factory.create(
            id="WO-1",
            asset_id="P-201",
            description="Repair pump bearing",
        )
        assert wo.id == "WO-1"
        assert wo.type == WorkOrderType.CORRECTIVE
        assert wo.priority == Priority.MEDIUM
        assert wo.status == WorkOrderStatus.CREATED

    def test_create_with_custom_type_and_priority(self) -> None:
        factory = WorkOrderFactory()
        wo = factory.create(
            id="WO-2",
            asset_id="P-201",
            type=WorkOrderType.PREDICTIVE,
            priority=Priority.HIGH,
            failure_mode="BEAR-WEAR-01",
        )
        assert wo.type == WorkOrderType.PREDICTIVE
        assert wo.priority == Priority.HIGH
        assert wo.failure_mode == "BEAR-WEAR-01"


class TestMaintenanceScheduler:
    """Test due date calculation."""

    def test_next_due_date(self) -> None:
        scheduler = MaintenanceScheduler()
        plan = MaintenancePlan(
            id="MP-1",
            asset_id="P-1",
            name="Quarterly",
            interval=Interval(months=3),
        )
        due = scheduler.next_due_date(plan, last_executed=date(2026, 1, 1))
        assert due == date(2026, 4, 1)

    def test_is_overdue_true(self) -> None:
        scheduler = MaintenanceScheduler()
        plan = MaintenancePlan(
            id="MP-1",
            asset_id="P-1",
            name="Monthly",
            interval=Interval(months=1),
        )
        assert scheduler.is_overdue(plan, date(2026, 1, 1), today=date(2026, 3, 1))

    def test_is_overdue_false(self) -> None:
        scheduler = MaintenanceScheduler()
        plan = MaintenancePlan(
            id="MP-1",
            asset_id="P-1",
            name="Monthly",
            interval=Interval(months=1),
        )
        assert not scheduler.is_overdue(plan, date(2026, 1, 1), today=date(2026, 1, 15))
