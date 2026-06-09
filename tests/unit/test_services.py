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

    def test_auto_id_is_deterministic(self) -> None:
        """When no id is supplied, the auto-generated id is a stable content
        hash — not a random uuid. Two identical create() calls (e.g. the same
        alarm fired twice, or a workflow re-run) yield the same id, so the CMMS
        can dedup instead of accumulating duplicate work orders."""
        factory = WorkOrderFactory()
        a = factory.create(
            asset_id="P-201", type=WorkOrderType.CORRECTIVE, description="Replace bearing"
        )
        b = factory.create(
            asset_id="P-201", type=WorkOrderType.CORRECTIVE, description="Replace bearing"
        )
        assert a.id.startswith("WO-AUTO-")
        assert a.id == b.id

    def test_auto_id_differs_by_content(self) -> None:
        factory = WorkOrderFactory()
        a = factory.create(asset_id="P-201", description="Replace bearing")
        b = factory.create(asset_id="P-202", description="Replace bearing")
        assert a.id != b.id

    def test_auto_id_enum_string_equivalence(self) -> None:
        """The agent runtime passes raw strings, the factory passes enums; both
        must yield the same id so the CMMS dedups instead of duplicating."""
        from machina.domain.services.work_order_factory import auto_work_order_id

        with_enum = auto_work_order_id("P-201", WorkOrderType.CORRECTIVE, Priority.HIGH, "x")
        with_str = auto_work_order_id("P-201", "corrective", "high", "x")
        assert with_enum == with_str

    def test_empty_session_reproduces_content_only_id(self) -> None:
        """U7 characterisation: an empty session_id keeps the pre-U7 digest."""
        from machina.domain.services.work_order_factory import auto_work_order_id

        content_only = auto_work_order_id("P-201", "corrective", "high", "x")
        explicit_empty = auto_work_order_id("P-201", "corrective", "high", "x", session_id="")
        assert content_only == explicit_empty

    def test_autonomous_path_dedups_same_content(self) -> None:
        """U7: the same alarm fired twice (no session) → one WO id."""
        from machina.domain.services.work_order_factory import auto_work_order_id

        first = auto_work_order_id("P-201", "corrective", "high", "bearing wear")
        second = auto_work_order_id("P-201", "corrective", "high", "bearing wear")
        assert first == second

    def test_same_session_same_content_dedups(self) -> None:
        """U7: a reworded retry in the same chat collapses to one WO id."""
        from machina.domain.services.work_order_factory import auto_work_order_id

        a = auto_work_order_id("P-201", "corrective", "high", "x", session_id="chat-42")
        b = auto_work_order_id("P-201", "corrective", "high", "x", session_id="chat-42")
        assert a == b

    def test_different_sessions_diverge(self) -> None:
        """U7: the same content in a later session is a distinct WO, not a collision."""
        from machina.domain.services.work_order_factory import auto_work_order_id

        s1 = auto_work_order_id("P-201", "corrective", "high", "x", session_id="chat-1")
        s2 = auto_work_order_id("P-201", "corrective", "high", "x", session_id="chat-2")
        assert s1 != s2
        # …and both differ from the content-only (autonomous) id.
        assert s1 != auto_work_order_id("P-201", "corrective", "high", "x")


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
