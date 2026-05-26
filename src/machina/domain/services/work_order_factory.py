"""WorkOrderFactory — create work orders with auto-populated fields.

Automatically fills in related failure modes, required skills,
spare parts, and estimated durations based on domain knowledge.
"""

from __future__ import annotations

import uuid
from typing import Any

from machina.domain.work_order import Priority, WorkOrder, WorkOrderType


def _auto_work_order_id() -> str:
    """Generate a workflow-friendly Work Order id.

    Used when the caller does not supply an ``id``.  ``WorkOrder.id``
    is required (non-empty) by pydantic validation, so a factory call
    with no id would raise ``ValidationError`` — the live-mode
    failure observed during the ``alarm_to_workorder`` regression.
    """
    return f"WO-AUTO-{uuid.uuid4().hex[:8].upper()}"


class WorkOrderFactory:
    """Factory for creating pre-filled work orders from alarms or plans.

    Future versions will use the failure analyzer and spare-part
    inventory to auto-populate fields.
    """

    def create(
        self,
        *,
        id: str = "",
        asset_id: str = "",
        type: WorkOrderType = WorkOrderType.CORRECTIVE,
        priority: Priority = Priority.MEDIUM,
        description: str = "",
        failure_mode: str | None = None,
        **kwargs: Any,
    ) -> WorkOrder:
        """Create a work order with sensible defaults.

        Args:
            id: Work order identifier.  Auto-generated as
                ``WO-AUTO-<hex>`` when not supplied — required because
                ``WorkOrder.id`` is validated non-empty.
            asset_id: Target asset.
            type: Maintenance type.
            priority: Urgency level.
            description: Free-text summary.
            failure_mode: Optional failure mode code.

        Returns:
            A new ``WorkOrder`` instance.
        """
        return WorkOrder(
            id=id or _auto_work_order_id(),
            type=type,
            priority=priority,
            asset_id=asset_id,
            description=description,
            failure_mode=failure_mode,
        )

    def create_batch(
        self,
        *,
        work_orders: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> list[WorkOrder]:
        """Create multiple work orders in one call.

        Each item in *work_orders* is a dict of keyword arguments
        forwarded to :meth:`create`.

        Args:
            work_orders: List of per-WO keyword-argument dicts.

        Returns:
            List of created ``WorkOrder`` instances.
        """
        work_orders = work_orders or []
        return [self.create(**wo) for wo in work_orders]
