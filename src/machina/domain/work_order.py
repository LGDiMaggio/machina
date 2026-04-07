"""WorkOrder entity — a maintenance task with lifecycle management.

Work orders track corrective, preventive, and predictive maintenance
activities through their full lifecycle from creation to closure.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkOrderType(StrEnum):
    """Classification of the maintenance activity."""

    CORRECTIVE = "corrective"
    PREVENTIVE = "preventive"
    PREDICTIVE = "predictive"
    IMPROVEMENT = "improvement"


class Priority(StrEnum):
    """Work order urgency level."""

    EMERGENCY = "emergency"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WorkOrderStatus(StrEnum):
    """Lifecycle state of a work order."""

    CREATED = "created"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CLOSED = "closed"
    CANCELLED = "cancelled"


_VALID_TRANSITIONS: dict[WorkOrderStatus, set[WorkOrderStatus]] = {
    WorkOrderStatus.CREATED: {WorkOrderStatus.ASSIGNED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.ASSIGNED: {
        WorkOrderStatus.IN_PROGRESS,
        WorkOrderStatus.CANCELLED,
    },
    WorkOrderStatus.IN_PROGRESS: {
        WorkOrderStatus.COMPLETED,
        WorkOrderStatus.CANCELLED,
    },
    WorkOrderStatus.COMPLETED: {WorkOrderStatus.CLOSED},
    WorkOrderStatus.CLOSED: set(),
    WorkOrderStatus.CANCELLED: set(),
}


class SparePartRequirement(BaseModel):
    """A spare part needed for a work order."""

    sku: str
    qty: int = Field(ge=1)


class WorkOrder(BaseModel):
    """A maintenance work order with lifecycle tracking.

    Supports status transitions, SLA tracking, and links to the
    asset, failure mode, and required spare parts.
    """

    id: str = Field(..., description="Work order identifier (e.g. 'WO-2026-1842')")
    type: WorkOrderType = Field(..., description="Type of maintenance activity")
    priority: Priority = Field(default=Priority.MEDIUM, description="Urgency level")
    status: WorkOrderStatus = Field(
        default=WorkOrderStatus.CREATED, description="Current lifecycle state"
    )
    asset_id: str = Field(..., description="Related asset identifier")
    description: str = Field(default="", description="Free-text description")
    failure_mode: str | None = Field(default=None, description="Associated failure mode code")
    requested_skills: list[str] = Field(
        default_factory=list, description="Skills required to execute"
    )
    estimated_duration_hours: float | None = Field(
        default=None, ge=0, description="Estimated duration in hours"
    )
    spare_parts: list[SparePartRequirement] = Field(
        default_factory=list, description="Required spare parts"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    assigned_to: str | None = Field(default=None, description="Assigned technician")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": False, "str_strip_whitespace": True}

    def transition_to(self, new_status: WorkOrderStatus) -> None:
        """Transition the work order to a new status.

        Raises:
            ValueError: If the transition is not allowed.
        """
        allowed = _VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            msg = f"Cannot transition from {self.status.value!r} to {new_status.value!r}"
            raise ValueError(msg)
        self.status = new_status
        self.updated_at = datetime.utcnow()
