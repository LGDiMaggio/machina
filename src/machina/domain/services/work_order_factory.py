"""WorkOrderFactory — create work orders with auto-populated fields.

Automatically fills in related failure modes, required skills,
spare parts, and estimated durations based on domain knowledge.
"""

from __future__ import annotations

from machina.domain.work_order import Priority, WorkOrder, WorkOrderType


class WorkOrderFactory:
    """Factory for creating pre-filled work orders from alarms or plans.

    Future versions will use the failure analyzer and spare-part
    inventory to auto-populate fields.
    """

    def create(
        self,
        *,
        id: str,
        asset_id: str,
        type: WorkOrderType = WorkOrderType.CORRECTIVE,
        priority: Priority = Priority.MEDIUM,
        description: str = "",
        failure_mode: str | None = None,
    ) -> WorkOrder:
        """Create a work order with sensible defaults.

        Args:
            id: Work order identifier.
            asset_id: Target asset.
            type: Maintenance type.
            priority: Urgency level.
            description: Free-text summary.
            failure_mode: Optional failure mode code.

        Returns:
            A new ``WorkOrder`` instance.
        """
        return WorkOrder(
            id=id,
            type=type,
            priority=priority,
            asset_id=asset_id,
            description=description,
            failure_mode=failure_mode,
        )
