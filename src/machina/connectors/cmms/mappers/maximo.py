"""Maximo payload ↔ Machina domain entity mapping (pure functions).

Extracted from :mod:`machina.connectors.cmms.maximo` so the mapping
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
    "MAXIMO_PRIORITY_MAP",
    "MAXIMO_STATUS_MAP",
    "MAXIMO_WORKTYPE_MAP",
    "REVERSE_MAXIMO_STATUS",
    "parse_asset",
    "parse_datetime",
    "parse_maintenance_plan",
    "parse_spare_part",
    "parse_work_order",
    "resolve_asset_type",
    "reverse_priority",
    "reverse_status",
    "reverse_worktype",
]


# ---------------------------------------------------------------------------
# Mapping constants
# ---------------------------------------------------------------------------

MAXIMO_PRIORITY_MAP: dict[int, Priority] = {
    1: Priority.EMERGENCY,
    2: Priority.HIGH,
    3: Priority.MEDIUM,
    4: Priority.LOW,
}

MAXIMO_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "WAPPR": WorkOrderStatus.CREATED,
    "APPR": WorkOrderStatus.ASSIGNED,
    "INPRG": WorkOrderStatus.IN_PROGRESS,
    "COMP": WorkOrderStatus.COMPLETED,
    "CLOSE": WorkOrderStatus.CLOSED,
    "CAN": WorkOrderStatus.CANCELLED,
}

MAXIMO_WORKTYPE_MAP: dict[str, WorkOrderType] = {
    "CM": WorkOrderType.CORRECTIVE,
    "PM": WorkOrderType.PREVENTIVE,
    "CP": WorkOrderType.PREDICTIVE,
    "EV": WorkOrderType.IMPROVEMENT,
}

REVERSE_MAXIMO_STATUS: dict[WorkOrderStatus, str] = {
    WorkOrderStatus.CREATED: "WAPPR",
    WorkOrderStatus.ASSIGNED: "APPR",
    WorkOrderStatus.IN_PROGRESS: "INPRG",
    WorkOrderStatus.COMPLETED: "COMP",
    WorkOrderStatus.CLOSED: "CLOSE",
    WorkOrderStatus.CANCELLED: "CAN",
}


# ---------------------------------------------------------------------------
# Public parse functions
# ---------------------------------------------------------------------------


def resolve_asset_type(
    data: dict[str, Any],
    asset_type_map: dict[str, AssetType] | None,
) -> AssetType:
    """Resolve Machina ``AssetType`` from a Maximo MXASSET record.

    Maximo does not expose a canonical category field. When the caller
    supplies an ``asset_type_map`` keyed by ``classstructureid`` (or
    ``assettype``), the resolver performs an exact lookup with a
    fallback to :attr:`AssetType.ROTATING_EQUIPMENT`. Without a map,
    every asset collapses to ``ROTATING_EQUIPMENT`` — the historical
    behaviour.
    """
    if not asset_type_map:
        return AssetType.ROTATING_EQUIPMENT
    key = str(data.get("classstructureid") or data.get("assettype") or "")
    return asset_type_map.get(key, AssetType.ROTATING_EQUIPMENT)


def parse_asset(
    data: dict[str, Any],
    asset_type_map: dict[str, AssetType] | None = None,
) -> Asset:
    """Convert a Maximo MXASSET JSON object to a Machina :class:`Asset`.

    Args:
        data: Parsed JSON record from the Maximo MXASSET object structure.
        asset_type_map: Optional mapping from Maximo ``classstructureid``
            (or ``assettype``) values to Machina :class:`AssetType`.
            When ``None`` all assets are classified as
            :attr:`AssetType.ROTATING_EQUIPMENT`.
    """
    return Asset(
        id=str(data.get("assetnum", "")),
        name=str(data.get("description", "")),
        type=resolve_asset_type(data, asset_type_map),
        location=str(data.get("location", "")),
        manufacturer=str(data.get("manufacturer", "")),
        model=str(data.get("modelnum", "")),
        serial_number=str(data.get("serialnum", "")),
        criticality=_maximo_criticality(data.get("priority", 0)),
        parent=data.get("parent") or None,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "assetnum",
                "description",
                "location",
                "manufacturer",
                "modelnum",
                "serialnum",
                "priority",
                "parent",
            }
        },
    )


def parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert a Maximo MXWO JSON object to a :class:`WorkOrder`."""
    raw_priority = data.get("wopriority", 3)
    try:
        prio_int = int(raw_priority)
    except (TypeError, ValueError):
        prio_int = 3
    priority = MAXIMO_PRIORITY_MAP.get(prio_int, Priority.MEDIUM)

    raw_status = str(data.get("status", "")).upper()
    status = MAXIMO_STATUS_MAP.get(raw_status, WorkOrderStatus.CREATED)

    raw_type = str(data.get("worktype", "")).upper()
    wo_type = MAXIMO_WORKTYPE_MAP.get(raw_type, WorkOrderType.CORRECTIVE)

    now = datetime.now(tz=UTC)
    created = data.get("reportdate", data.get("changedate", ""))
    updated = data.get("changedate", "")

    return WorkOrder(
        id=str(data.get("wonum", "")),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=str(data.get("assetnum", "")),
        description=str(data.get("description", "")),
        assigned_to=data.get("lead") or data.get("assignedownergroup") or None,
        failure_mode=data.get("failurecode") or None,
        failure_cause=data.get("failureremark") or data.get("problemcode") or None,
        created_at=parse_datetime(created) if created else now,
        updated_at=parse_datetime(updated) if updated else now,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "wonum",
                "wopriority",
                "status",
                "worktype",
                "assetnum",
                "description",
                "lead",
                "assignedownergroup",
                "failurecode",
                "failureremark",
                "problemcode",
                "reportdate",
                "changedate",
            }
        },
    )


def parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert a Maximo MXINVENTORY JSON object to a :class:`SparePart`."""
    return SparePart(
        sku=str(data.get("itemnum", "")),
        name=str(data.get("description", data.get("item", {}).get("description", ""))),
        stock_quantity=int(data.get("curbal", 0)),
        reorder_point=int(data.get("reorder", 0)),
        unit_cost=float(data.get("avgcost", data.get("lastcost", 0.0))),
        warehouse_location=str(data.get("location", "")),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "itemnum",
                "description",
                "item",
                "curbal",
                "reorder",
                "avgcost",
                "lastcost",
                "location",
            }
        },
    )


def parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert a Maximo MXPM JSON object to a :class:`MaintenancePlan`."""
    freq_days = int(data.get("frequency", 0))
    return MaintenancePlan(
        id=str(data.get("pmnum", "")),
        asset_id=str(data.get("assetnum", "")),
        name=str(data.get("description", "")),
        interval=Interval(days=freq_days),
        active=str(data.get("status", "")).upper() == "ACTIVE",
    )


def parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 date string into a timezone-aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Public reverse (domain → Maximo payload) functions
# ---------------------------------------------------------------------------


def reverse_priority(priority: Priority) -> int:
    """Map Machina priority back to Maximo integer (1-4)."""
    return {
        Priority.EMERGENCY: 1,
        Priority.HIGH: 2,
        Priority.MEDIUM: 3,
        Priority.LOW: 4,
    }.get(priority, 3)


def reverse_worktype(wo_type: WorkOrderType) -> str:
    """Map Machina work-order type to Maximo work type code."""
    return {
        WorkOrderType.CORRECTIVE: "CM",
        WorkOrderType.PREVENTIVE: "PM",
        WorkOrderType.PREDICTIVE: "CP",
        WorkOrderType.IMPROVEMENT: "EV",
    }.get(wo_type, "CM")


def reverse_status(status: WorkOrderStatus) -> str:
    """Map Machina work-order status to Maximo status code."""
    return REVERSE_MAXIMO_STATUS.get(status, "WAPPR")


# ---------------------------------------------------------------------------
# Module-private helpers (only used by parse_* / reverse_*)
# ---------------------------------------------------------------------------


def _maximo_criticality(priority_val: Any) -> Criticality:
    """Map Maximo numeric priority (1-3 → A/B/C)."""
    try:
        val = int(priority_val)
    except (TypeError, ValueError):
        return Criticality.C
    if val <= 1:
        return Criticality.A
    if val <= 2:
        return Criticality.B
    return Criticality.C
