"""Tests for the MaintenancePlan domain entity."""

from machina.domain.maintenance_plan import Interval, MaintenancePlan


class TestInterval:
    """Test Interval duration calculations."""

    def test_monthly_interval(self) -> None:
        interval = Interval(months=3)
        assert interval.total_days == 90

    def test_weekly_interval(self) -> None:
        interval = Interval(weeks=2)
        assert interval.total_days == 14

    def test_combined_interval(self) -> None:
        interval = Interval(days=5, weeks=1, months=1)
        assert interval.total_days == 42  # 5 + 7 + 30

    def test_zero_interval(self) -> None:
        interval = Interval()
        assert interval.total_days == 0


class TestMaintenancePlan:
    """Test MaintenancePlan creation."""

    def test_create_plan(self, sample_maintenance_plan: MaintenancePlan) -> None:
        assert sample_maintenance_plan.id == "MP-P201-QUARTERLY"
        assert sample_maintenance_plan.interval.months == 3
        assert len(sample_maintenance_plan.tasks) == 4
        assert sample_maintenance_plan.active is True

    def test_plan_defaults(self) -> None:
        plan = MaintenancePlan(
            id="MP-1",
            asset_id="P-1",
            name="Monthly Check",
            interval=Interval(months=1),
        )
        assert plan.active is True
        assert plan.tasks == []
        assert plan.required_skills == []

    def test_serialization_roundtrip(self, sample_maintenance_plan: MaintenancePlan) -> None:
        data = sample_maintenance_plan.model_dump()
        restored = MaintenancePlan.model_validate(data)
        assert restored.id == sample_maintenance_plan.id
        assert restored.interval.months == sample_maintenance_plan.interval.months
