"""Tests for the WorkOrder domain entity."""

import pytest

from machina.domain.work_order import (
    Priority,
    SparePartRequirement,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)


class TestWorkOrder:
    """Test WorkOrder creation and lifecycle."""

    def test_create_work_order(self, sample_work_order: WorkOrder) -> None:
        assert sample_work_order.id == "WO-2026-1842"
        assert sample_work_order.type == WorkOrderType.CORRECTIVE
        assert sample_work_order.priority == Priority.HIGH
        assert sample_work_order.status == WorkOrderStatus.CREATED

    def test_default_priority_is_medium(self) -> None:
        wo = WorkOrder(id="WO-1", type=WorkOrderType.PREVENTIVE, asset_id="P-1")
        assert wo.priority == Priority.MEDIUM

    def test_status_defaults_to_created(self) -> None:
        wo = WorkOrder(id="WO-1", type=WorkOrderType.PREVENTIVE, asset_id="P-1")
        assert wo.status == WorkOrderStatus.CREATED

    def test_transition_created_to_assigned(self, sample_work_order: WorkOrder) -> None:
        sample_work_order.transition_to(WorkOrderStatus.ASSIGNED)
        assert sample_work_order.status == WorkOrderStatus.ASSIGNED

    def test_transition_assigned_to_in_progress(self, sample_work_order: WorkOrder) -> None:
        sample_work_order.transition_to(WorkOrderStatus.ASSIGNED)
        sample_work_order.transition_to(WorkOrderStatus.IN_PROGRESS)
        assert sample_work_order.status == WorkOrderStatus.IN_PROGRESS

    def test_full_lifecycle(self, sample_work_order: WorkOrder) -> None:
        sample_work_order.transition_to(WorkOrderStatus.ASSIGNED)
        sample_work_order.transition_to(WorkOrderStatus.IN_PROGRESS)
        sample_work_order.transition_to(WorkOrderStatus.COMPLETED)
        sample_work_order.transition_to(WorkOrderStatus.CLOSED)
        assert sample_work_order.status == WorkOrderStatus.CLOSED

    def test_invalid_transition_raises(self, sample_work_order: WorkOrder) -> None:
        with pytest.raises(ValueError, match="Cannot transition"):
            sample_work_order.transition_to(WorkOrderStatus.COMPLETED)

    def test_cancelled_is_terminal(self, sample_work_order: WorkOrder) -> None:
        sample_work_order.transition_to(WorkOrderStatus.CANCELLED)
        with pytest.raises(ValueError):
            sample_work_order.transition_to(WorkOrderStatus.CREATED)

    def test_closed_is_terminal(self, sample_work_order: WorkOrder) -> None:
        sample_work_order.transition_to(WorkOrderStatus.ASSIGNED)
        sample_work_order.transition_to(WorkOrderStatus.IN_PROGRESS)
        sample_work_order.transition_to(WorkOrderStatus.COMPLETED)
        sample_work_order.transition_to(WorkOrderStatus.CLOSED)
        with pytest.raises(ValueError):
            sample_work_order.transition_to(WorkOrderStatus.CREATED)

    def test_serialization_roundtrip(self, sample_work_order: WorkOrder) -> None:
        data = sample_work_order.model_dump()
        restored = WorkOrder.model_validate(data)
        assert restored.id == sample_work_order.id
        assert restored.type == sample_work_order.type

    def test_spare_parts(self) -> None:
        wo = WorkOrder(
            id="WO-1",
            type=WorkOrderType.CORRECTIVE,
            asset_id="P-1",
            spare_parts=[SparePartRequirement(sku="SKF-6310", qty=2)],
        )
        assert len(wo.spare_parts) == 1
        assert wo.spare_parts[0].sku == "SKF-6310"


class TestWorkOrderType:
    """Test WorkOrderType enum values."""

    def test_all_types(self) -> None:
        expected = {"corrective", "preventive", "predictive", "improvement"}
        assert {t.value for t in WorkOrderType} == expected


class TestPriority:
    """Test Priority enum values."""

    def test_all_priorities(self) -> None:
        expected = {"emergency", "high", "medium", "low"}
        assert {p.value for p in Priority} == expected
