"""UpKeep payload ↔ Machina domain entity mapping (pure functions).

Extracted from :mod:`machina.connectors.cmms.upkeep` so the mapping
logic is testable on raw ``dict`` inputs — no HTTP mocks, no connector
state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)

__all__ = [
    "REVERSE_UPKEEP_STATUS",
    "UPKEEP_CATEGORY_MAP",
    "UPKEEP_PRIORITY_MAP",
    "UPKEEP_STATUS_MAP",
    "parse_asset",
    "parse_datetime",
    "parse_maintenance_plan",
    "parse_spare_part",
    "parse_work_order",
    "reverse_priority",
    "reverse_status",
]


# ---------------------------------------------------------------------------
# Mapping constants
# ---------------------------------------------------------------------------

UPKEEP_PRIORITY_MAP: dict[int, Priority] = {
    # Per the UpKeep REST API v2, priority is a 0-indexed integer where
    # 0 is the lowest and 3 is the highest. See:
    # https://developers.onupkeep.com/#work-orders
    0: Priority.LOW,
    1: Priority.MEDIUM,
    2: Priority.HIGH,
    3: Priority.EMERGENCY,
}

UPKEEP_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "open": WorkOrderStatus.CREATED,
    "in progress": WorkOrderStatus.IN_PROGRESS,
    "on hold": WorkOrderStatus.ASSIGNED,
    "complete": WorkOrderStatus.COMPLETED,
}

REVERSE_UPKEEP_STATUS: dict[WorkOrderStatus, str] = {
    WorkOrderStatus.CREATED: "open",
    WorkOrderStatus.ASSIGNED: "on hold",
    WorkOrderStatus.IN_PROGRESS: "in progress",
    WorkOrderStatus.COMPLETED: "complete",
    WorkOrderStatus.CLOSED: "complete",  # UpKeep has no distinct closed state
    WorkOrderStatus.CANCELLED: "on hold",  # UpKeep has no distinct cancelled state
}

UPKEEP_CATEGORY_MAP: dict[str, AssetType] = {
    "Rotating Equipment": AssetType.ROTATING_EQUIPMENT,
    "Static Equipment": AssetType.STATIC_EQUIPMENT,
    "Instrument": AssetType.INSTRUMENT,
    "Electrical": AssetType.ELECTRICAL,
    "Piping": AssetType.PIPING,
    "HVAC": AssetType.HVAC,
    "Safety": AssetType.SAFETY,
}


# ---------------------------------------------------------------------------
# Public parse functions
# ---------------------------------------------------------------------------


def parse_asset(data: dict[str, Any]) -> Asset:
    """Convert an UpKeep asset JSON object to a Machina :class:`Asset`."""
    category = str(data.get("category", ""))
    return Asset(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        type=UPKEEP_CATEGORY_MAP.get(category, AssetType.ROTATING_EQUIPMENT),
        location=str(data.get("location", "")),
        manufacturer=str(data.get("make", "")),
        model=str(data.get("model", "")),
        serial_number=str(data.get("serialNumber", "")),
        criticality=Criticality.C,
        parent=data.get("parentAssetId"),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "name",
                "category",
                "location",
                "make",
                "model",
                "serialNumber",
                "parentAssetId",
            }
        },
    )


def parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert an UpKeep work-order JSON object to a :class:`WorkOrder`."""
    raw_priority = data.get("priority", 1)
    priority = UPKEEP_PRIORITY_MAP.get(int(raw_priority), Priority.MEDIUM)
    raw_status = str(data.get("status", "open")).lower()
    status = UPKEEP_STATUS_MAP.get(raw_status, WorkOrderStatus.CREATED)
    wo_type = (
        WorkOrderType.PREVENTIVE
        if data.get("category") == "preventive"
        else WorkOrderType.CORRECTIVE
    )
    created = data.get("createdAt", "")
    updated = data.get("updatedAt", "")
    now = datetime.now(tz=UTC)
    return WorkOrder(
        id=str(data.get("id", "")),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=str(data.get("assetId") or data.get("asset", "")),
        description=str(data.get("title", "")),
        assigned_to=data.get("assignedToId"),
        created_at=parse_datetime(created) if created else now,
        updated_at=parse_datetime(updated) if updated else now,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "priority",
                "status",
                "category",
                "createdAt",
                "updatedAt",
                "assetId",
                "asset",
                "title",
                "assignedToId",
            }
        },
    )


def parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert an UpKeep part JSON object to a :class:`SparePart`.

    Prefers the physical part identifier (``partNumber`` then ``barcode``)
    as the SKU, falling back to UpKeep's internal record ``id`` only
    when neither is provided.
    """
    sku = str(data.get("partNumber") or data.get("barcode") or data.get("id") or "")
    return SparePart(
        sku=sku,
        name=str(data.get("name", "")),
        stock_quantity=int(data.get("quantity", 0)),
        unit_cost=float(data.get("cost", 0.0)),
        warehouse_location=str(data.get("area", "")),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "partNumber",
                "barcode",
                "name",
                "quantity",
                "cost",
                "area",
            }
        },
    )


def parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert an UpKeep preventive-maintenance JSON to a :class:`MaintenancePlan`."""
    freq_days = int(data.get("frequencyDays", 0))
    return MaintenancePlan(
        id=str(data.get("id", "")),
        asset_id=str(data.get("assetId") or ""),
        name=str(data.get("title", "")),
        interval=Interval(days=freq_days),
        tasks=[str(t) for t in data.get("tasks", [])],
        active=data.get("status", "active") == "active",
    )


def parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 date string into a timezone-aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Public reverse (domain → UpKeep payload) functions
# ---------------------------------------------------------------------------


def reverse_priority(priority: Priority) -> int:
    """Map Machina priority back to UpKeep integer (0-indexed, 0-3).

    UpKeep's REST API v2 uses a 0-indexed priority scale where 0 is the
    lowest and 3 is the highest. See
    https://developers.onupkeep.com/#work-orders.
    """
    return {
        Priority.LOW: 0,
        Priority.MEDIUM: 1,
        Priority.HIGH: 2,
        Priority.EMERGENCY: 3,
    }.get(priority, 1)


def reverse_status(status: WorkOrderStatus) -> str:
    """Map Machina work-order status to UpKeep status string."""
    return REVERSE_UPKEEP_STATUS.get(status, "open")
