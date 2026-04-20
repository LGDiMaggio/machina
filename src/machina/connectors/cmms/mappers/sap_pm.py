"""SAP PM payload ↔ Machina domain entity mapping (pure functions).

Extracted from :mod:`machina.connectors.cmms.sap_pm` so the mapping
logic is testable on raw ``dict`` inputs — no HTTP mocks, no connector
state.  Public ``parse_*`` / ``reverse_*`` functions and mapping
constants form the module-level API; helpers prefixed with ``_`` are
private to this module.
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
    "REVERSE_SAP_STATUS",
    "SAP_EQUIP_CATEGORY_MAP",
    "SAP_ORDER_TYPE_MAP",
    "SAP_PRIORITY_MAP",
    "SAP_STATUS_MAP",
    "parse_asset",
    "parse_maintenance_plan",
    "parse_sap_datetime",
    "parse_spare_part",
    "parse_work_order",
    "reverse_order_type",
    "reverse_priority",
    "reverse_status",
]


# ---------------------------------------------------------------------------
# Mapping constants
# ---------------------------------------------------------------------------

SAP_ORDER_TYPE_MAP: dict[str, WorkOrderType] = {
    "PM01": WorkOrderType.CORRECTIVE,
    "PM02": WorkOrderType.PREVENTIVE,
    "PM03": WorkOrderType.PREDICTIVE,
    "PM04": WorkOrderType.IMPROVEMENT,
}

SAP_PRIORITY_MAP: dict[str, Priority] = {
    "1": Priority.EMERGENCY,
    "2": Priority.HIGH,
    "3": Priority.MEDIUM,
    "4": Priority.LOW,
}

SAP_STATUS_MAP: dict[str, WorkOrderStatus] = {
    "CRTD": WorkOrderStatus.CREATED,
    "REL": WorkOrderStatus.ASSIGNED,
    "PCNF": WorkOrderStatus.IN_PROGRESS,
    "CNF": WorkOrderStatus.COMPLETED,
    "TECO": WorkOrderStatus.CLOSED,
    "CLSD": WorkOrderStatus.CLOSED,
    "DLFL": WorkOrderStatus.CANCELLED,
}

REVERSE_SAP_STATUS: dict[WorkOrderStatus, str] = {
    WorkOrderStatus.CREATED: "CRTD",
    WorkOrderStatus.ASSIGNED: "REL",
    WorkOrderStatus.IN_PROGRESS: "PCNF",
    WorkOrderStatus.COMPLETED: "CNF",
    WorkOrderStatus.CLOSED: "TECO",
    WorkOrderStatus.CANCELLED: "DLFL",
}

SAP_EQUIP_CATEGORY_MAP: dict[str, AssetType] = {
    "M": AssetType.ROTATING_EQUIPMENT,  # Machinery
    "E": AssetType.ELECTRICAL,  # Electrical
    "I": AssetType.INSTRUMENT,  # Instrumentation
    "P": AssetType.PIPING,  # Piping
    "H": AssetType.HVAC,  # HVAC
    "S": AssetType.SAFETY,  # Safety
}


# ---------------------------------------------------------------------------
# Public parse functions
# ---------------------------------------------------------------------------


def parse_asset(data: dict[str, Any]) -> Asset:
    """Convert SAP Equipment OData entity to a Machina :class:`Asset`."""
    cat = str(data.get("EquipmentCategory", ""))
    return Asset(
        id=str(data.get("Equipment", data.get("EquipmentNumber", ""))),
        name=str(data.get("EquipmentName", data.get("Description", ""))),
        type=SAP_EQUIP_CATEGORY_MAP.get(cat, AssetType.ROTATING_EQUIPMENT),
        location=str(data.get("FunctionalLocation", "")),
        manufacturer=str(data.get("Manufacturer", data.get("ManufacturerPartNmbr", ""))),
        model=str(data.get("ModelNumber", "")),
        serial_number=str(data.get("SerialNumber", data.get("ManufacturerSerialNumber", ""))),
        criticality=_sap_criticality(data.get("ABCIndicator", "")),
        parent=data.get("SuperordinateEquipment") or None,
        equipment_class_code=data.get("EquipmentClassCode") or None,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "Equipment",
                "EquipmentNumber",
                "EquipmentName",
                "Description",
                "EquipmentCategory",
                "FunctionalLocation",
                "Manufacturer",
                "ManufacturerPartNmbr",
                "ModelNumber",
                "SerialNumber",
                "ManufacturerSerialNumber",
                "ABCIndicator",
                "SuperordinateEquipment",
                "EquipmentClassCode",
            }
        },
    )


def parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert SAP MaintenanceOrder OData entity to a :class:`WorkOrder`."""
    raw_type = str(data.get("MaintenanceOrderType", ""))
    wo_type = SAP_ORDER_TYPE_MAP.get(raw_type, WorkOrderType.CORRECTIVE)

    raw_priority = str(data.get("MaintPriority", "3"))
    priority = SAP_PRIORITY_MAP.get(raw_priority, Priority.MEDIUM)

    # SAP uses system status; try multiple fields
    sys_status = str(data.get("MaintenanceOrderSystemStatus", data.get("SystemStatus", "")))
    status = _map_sap_status(sys_status)

    now = datetime.now(tz=UTC)
    created = data.get("CreationDate", data.get("MaintOrdBasicStartDate", ""))
    updated = data.get("LastChangeDateTime", data.get("MaintOrdBasicEndDate", ""))

    return WorkOrder(
        id=str(data.get("MaintenanceOrder", data.get("MaintenanceOrderNumber", ""))),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=str(data.get("Equipment", data.get("EquipmentNumber", ""))),
        description=str(data.get("MaintenanceOrderDesc", data.get("Description", ""))),
        assigned_to=data.get("MaintOrdPersonResponsible") or None,
        failure_mode=data.get("MaintenanceActivityType") or None,
        failure_cause=data.get("MaintenanceCause") or data.get("MaintNotifCause") or None,
        created_at=parse_sap_datetime(created) if created else now,
        updated_at=parse_sap_datetime(updated) if updated else now,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "MaintenanceOrder",
                "MaintenanceOrderNumber",
                "MaintenanceOrderType",
                "MaintPriority",
                "MaintenanceOrderSystemStatus",
                "SystemStatus",
                "Equipment",
                "EquipmentNumber",
                "MaintenanceOrderDesc",
                "Description",
                "MaintOrdPersonResponsible",
                "MaintenanceActivityType",
                "MaintenanceCause",
                "MaintNotifCause",
                "CreationDate",
                "MaintOrdBasicStartDate",
                "LastChangeDateTime",
                "MaintOrdBasicEndDate",
            }
        },
    )


def parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert SAP material / BOM component data to a :class:`SparePart`."""
    return SparePart(
        sku=str(data.get("Material", data.get("MaterialNumber", ""))),
        name=str(data.get("MaterialDescription", data.get("Description", ""))),
        stock_quantity=int(data.get("AvailableQuantity", data.get("Quantity", 0))),
        unit_cost=float(data.get("StandardPrice", data.get("Price", 0.0))),
        warehouse_location=str(data.get("StorageLocation", data.get("Plant", ""))),
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "Material",
                "MaterialNumber",
                "MaterialDescription",
                "Description",
                "AvailableQuantity",
                "Quantity",
                "StandardPrice",
                "Price",
                "StorageLocation",
                "Plant",
            }
        },
    )


def parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert SAP MaintenancePlan OData entity to a :class:`MaintenancePlan`."""
    cycle_val = int(data.get("MaintenancePlanCycleValue", data.get("CycleValue", 0)))
    cycle_unit = str(data.get("MaintenancePlanCycleUnit", data.get("CycleUnit", "DAY")))
    interval = _sap_cycle_to_interval(cycle_val, cycle_unit)

    return MaintenancePlan(
        id=str(data.get("MaintenancePlan", data.get("MaintenancePlanNumber", ""))),
        asset_id=str(data.get("Equipment", "")),
        name=str(data.get("MaintenancePlanDesc", data.get("Description", ""))),
        interval=interval,
        active=str(data.get("MaintenancePlanStatus", "")).upper() != "INAC",
    )


def parse_sap_datetime(value: str) -> datetime:
    """Parse SAP date/datetime strings into timezone-aware datetime.

    Handles ISO-8601, SAP ``/Date(millis)/`` format, and plain
    ``YYYY-MM-DD`` / ``YYYYMMDD`` dates.  Empty or unparseable input
    falls back to ``datetime.now(UTC)`` for backwards compatibility.
    """
    if not value:
        return datetime.now(tz=UTC)
    # SAP JSON /Date(1234567890000)/ format
    if value.startswith("/Date("):
        millis_str = value.replace("/Date(", "").replace(")/", "")
        # Handle timezone offset: /Date(1234567890000+0000)/
        if "+" in millis_str:
            millis_str = millis_str.split("+")[0]
        if "-" in millis_str and millis_str.index("-") > 0:
            millis_str = millis_str.split("-")[0]
        millis = int(millis_str)
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    # Standard ISO-8601
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass
    # Plain date YYYY-MM-DD or YYYYMMDD
    try:
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Public reverse (domain → SAP payload) functions
# ---------------------------------------------------------------------------


def reverse_priority(priority: Priority) -> str:
    """Map Machina priority to SAP priority code."""
    return {
        Priority.EMERGENCY: "1",
        Priority.HIGH: "2",
        Priority.MEDIUM: "3",
        Priority.LOW: "4",
    }.get(priority, "3")


def reverse_order_type(wo_type: WorkOrderType) -> str:
    """Map Machina work-order type to SAP order type."""
    return {
        WorkOrderType.CORRECTIVE: "PM01",
        WorkOrderType.PREVENTIVE: "PM02",
        WorkOrderType.PREDICTIVE: "PM03",
        WorkOrderType.IMPROVEMENT: "PM04",
    }.get(wo_type, "PM01")


def reverse_status(status: WorkOrderStatus) -> str:
    """Map Machina work-order status to SAP system status code."""
    return REVERSE_SAP_STATUS.get(status, "CRTD")


# ---------------------------------------------------------------------------
# Module-private helpers (only used by parse_* / reverse_*)
# ---------------------------------------------------------------------------


def _sap_criticality(abc_indicator: Any) -> Criticality:
    """Map SAP ABC indicator to Machina criticality."""
    val = str(abc_indicator).upper().strip()
    if val == "A":
        return Criticality.A
    if val == "B":
        return Criticality.B
    return Criticality.C


def _map_sap_status(sys_status: str) -> WorkOrderStatus:
    """Map SAP system status string to :class:`WorkOrderStatus`.

    SAP system status can be a compound string like ``"CRTD REL MANC"``.
    Tokens are checked in reverse lifecycle order so the most progressed
    state wins.
    """
    tokens = sys_status.upper().split()
    for token in ("DLFL", "CLSD", "TECO", "CNF", "PCNF", "REL", "CRTD"):
        if token in tokens:
            return SAP_STATUS_MAP[token]
    # Fallback: try direct lookup of the full string
    return SAP_STATUS_MAP.get(sys_status, WorkOrderStatus.CREATED)


def _sap_cycle_to_interval(value: int, unit: str) -> Interval:
    """Map SAP cycle value + unit to a Machina :class:`Interval`."""
    unit_upper = unit.upper().strip()
    if unit_upper in ("DAY", "TAG"):
        return Interval(days=value)
    if unit_upper in ("WK", "WOC"):
        return Interval(weeks=value)
    if unit_upper in ("MON", "MON."):
        return Interval(months=value)
    if unit_upper in ("H", "STD"):
        return Interval(hours=value)
    # Default to days
    return Interval(days=value)
