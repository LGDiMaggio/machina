"""WorkOrderFactory — create work orders with auto-populated fields.

Automatically fills in related failure modes, required skills,
spare parts, and estimated durations based on domain knowledge.
"""

from __future__ import annotations

import hashlib
from typing import Any

from machina.domain.work_order import Priority, WorkOrder, WorkOrderType


def auto_work_order_id(
    asset_id: str = "",
    type: WorkOrderType | str = WorkOrderType.CORRECTIVE,
    priority: Priority | str = Priority.MEDIUM,
    description: str = "",
) -> str:
    """Deterministic content-derived Work Order id (``WO-AUTO-<sha8>``).

    ``WorkOrder.id`` is required (non-empty) by pydantic validation, so a
    caller with no id needs one generated. We derive it from the work
    order's content rather than a random uuid so that repeated creation of
    the *same* logical work order — an alarm fired twice, a re-run
    workflow, or a model that re-requests the create tool inside the agent
    loop — collapses to a single id the CMMS can dedup, instead of minting
    a fresh id each time and accumulating duplicates.

    ``type``/``priority`` accept either the enum or its string value;
    ``StrEnum`` formats to its value, so the agent runtime (which passes
    raw strings) and the factory (which passes enums) produce identical
    ids for equivalent content.
    """
    digest = (
        hashlib.sha256(f"{asset_id}|{type}|{priority}|{description}".encode())
        .hexdigest()[:8]
        .upper()
    )
    return f"WO-AUTO-{digest}"


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
            id=id or auto_work_order_id(asset_id, type, priority, description),
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
