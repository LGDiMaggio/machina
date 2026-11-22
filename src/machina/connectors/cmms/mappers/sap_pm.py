"""SAP PM payload ↔ Machina domain entity mapping (pure functions).

Extracted from :mod:`machina.connectors.cmms.sap_pm` so the mapping
logic is testable on raw ``dict`` inputs — no HTTP mocks, no connector
state.  Public ``parse_*`` / ``reverse_*`` functions and mapping
constants form the module-level API; helpers prefixed with ``_`` are
private to this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.maintenance_plan import Interval, MaintenancePlan
from machina.domain.spare_part import SparePart
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "REVERSE_SAP_STATUS",
    "SAP_EQUIP_CATEGORY_MAP",
    "SAP_ORDER_TYPE_MAP",
    "SAP_PRIORITY_MAP",
    "SAP_STATUS_MAP",
    "SapFieldError",
    "order_etag",
    "parse_asset",
    "parse_maintenance_plan",
    "parse_sap_datetime",
    "parse_spare_part",
    "parse_work_order",
    "reverse_order_type",
    "reverse_priority",
    "reverse_status",
    "system_status_codes",
    "system_status_text",
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

# The properties each parse function maps to a domain field; the others go to
# the entity's ``metadata`` (see :func:`_metadata`).
_ASSET_PROPERTIES = frozenset(
    {
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
)
_WORK_ORDER_PROPERTIES = frozenset(
    {
        "MaintenanceOrder",
        "MaintenanceOrderNumber",
        "MaintenanceOrderType",
        "MaintPriority",
        "SystemStatusText",
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
)
_SPARE_PART_PROPERTIES = frozenset(
    {
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
)

# OData V2 members that describe an entry's transport, not the record:
# ``__metadata`` (``uri``, ``type``, ``etag``) and a deferred navigation
# property's ``__deferred`` (``uri``). A ``uri`` names the host and service path
# the SAP gateway sees itself on, often an internal one.
_TRANSPORT_MEMBERS = frozenset({"__metadata", "__deferred"})


class SapFieldError(ValueError):
    """A SAP property holds a value the mapper cannot read as its domain field.

    Raised for a number, a date, or text: an OData V2 ``Edm.Decimal`` arrives
    as text with its decimals (``"12.000"``), which is not a whole number, and
    an object where a field takes text is not read as its text. The message
    names the property, never its value, and the error is raised with no
    exception chained to it, since the conversion's own error quotes the value.

    Attributes:
        field: The SAP property, e.g. ``"AvailableQuantity"``.
        expected: What it should hold, e.g. ``"a whole number"``.
    """

    def __init__(self, field: str, expected: str) -> None:
        super().__init__(f"SAP property {field!r} is not {expected}")
        self.field = field
        self.expected = expected


# ---------------------------------------------------------------------------
# Public parse functions
# ---------------------------------------------------------------------------


def parse_asset(data: dict[str, Any]) -> Asset:
    """Convert SAP Equipment OData entity to a Machina :class:`Asset`.

    Properties no field takes go to ``metadata``, except OData transport
    members (see :func:`_metadata`).

    Raises:
        SapFieldError: If a property a text field takes holds an object or a
            list (see :func:`_text`).
    """
    cat = str(data.get("EquipmentCategory", ""))
    return Asset(
        id=_text(data, ("Equipment", "EquipmentNumber")),
        name=_text(data, ("EquipmentName", "Description")),
        type=SAP_EQUIP_CATEGORY_MAP.get(cat, AssetType.ROTATING_EQUIPMENT),
        location=_text(data, ("FunctionalLocation",)),
        manufacturer=_text(data, ("Manufacturer", "ManufacturerPartNmbr")),
        model=_text(data, ("ModelNumber",)),
        serial_number=_text(data, ("SerialNumber", "ManufacturerSerialNumber")),
        criticality=_sap_criticality(data.get("ABCIndicator", "")),
        parent=data.get("SuperordinateEquipment") or None,
        equipment_class_code=data.get("EquipmentClassCode") or None,
        metadata=_metadata(data, _ASSET_PROPERTIES),
    )


def parse_work_order(data: dict[str, Any]) -> WorkOrder:
    """Convert SAP MaintenanceOrder OData entity to a :class:`WorkOrder`.

    The status is mapped from ``SystemStatusText`` — the order's active
    system statuses as one line (see :func:`system_status_codes`). Properties
    no field takes go to ``metadata``, except OData transport members (see
    :func:`_metadata`).

    Raises:
        SapFieldError: If a date property holds a value
            :func:`parse_sap_datetime` cannot read (a ``/Date(...)/`` that is
            not a number of milliseconds a date can hold, a number), or a
            property a text field takes holds an object or a list.
    """
    raw_type = str(data.get("MaintenanceOrderType", ""))
    wo_type = SAP_ORDER_TYPE_MAP.get(raw_type, WorkOrderType.CORRECTIVE)

    raw_priority = str(data.get("MaintPriority", "3"))
    priority = SAP_PRIORITY_MAP.get(raw_priority, Priority.MEDIUM)

    status = _map_sap_status(str(data.get("SystemStatusText") or ""))

    now = datetime.now(tz=UTC)
    created_at = _date(data, ("CreationDate", "MaintOrdBasicStartDate"), now)
    updated_at = _date(data, ("LastChangeDateTime", "MaintOrdBasicEndDate"), now)

    return WorkOrder(
        id=_text(data, ("MaintenanceOrder", "MaintenanceOrderNumber")),
        type=wo_type,
        priority=priority,
        status=status,
        asset_id=_text(data, ("Equipment", "EquipmentNumber")),
        description=_text(data, ("MaintenanceOrderDesc", "Description")),
        assigned_to=data.get("MaintOrdPersonResponsible") or None,
        failure_mode=data.get("MaintenanceActivityType") or None,
        failure_cause=data.get("MaintenanceCause") or data.get("MaintNotifCause") or None,
        created_at=created_at,
        updated_at=updated_at,
        metadata=_metadata(data, _WORK_ORDER_PROPERTIES),
    )


def system_status_text(data: dict[str, Any]) -> str:
    """Return a SAP maintenance order's ``SystemStatusText``, ``""`` if it has none.

    ``API_MAINTENANCEORDER`` has no property holding one status: it reports
    the active system statuses in this one line, such as
    ``"CRTD MACM ORAP PRC"``. SAP limits it to 40 characters; when the
    statuses do not fit, it lists only those with the highest priority.
    """
    return str(data.get("SystemStatusText") or "")


def system_status_codes(data: dict[str, Any]) -> frozenset[str]:
    """Return the system status codes listed on a SAP maintenance order.

    The codes of :func:`system_status_text`, which may leave out
    lower-priority statuses that do not fit in its 40 characters.
    """
    return frozenset(system_status_text(data).upper().split())


def order_etag(data: dict[str, Any]) -> str | None:
    """Return the ETag of a maintenance order as an OData V2 read returns it.

    OData V2's JSON format carries an entry's ETag in ``__metadata.etag``.
    ``None`` when the entry has none.
    """
    metadata = data.get("__metadata")
    etag = metadata.get("etag") if isinstance(metadata, dict) else None
    return str(etag) if etag else None


def parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert SAP material / BOM component data to a :class:`SparePart`.

    Properties no field takes go to ``metadata``, except OData transport
    members (see :func:`_metadata`).

    Raises:
        SapFieldError: If the quantity is not a whole number (an
            ``Edm.Decimal`` such as ``"12.000"`` included), the price is
            not a number, or a property a text field takes holds an object or
            a list.
    """
    return SparePart(
        sku=_text(data, ("Material", "MaterialNumber")),
        name=_text(data, ("MaterialDescription", "Description")),
        stock_quantity=_convert(data, ("AvailableQuantity", "Quantity"), 0, int, _WHOLE_NUMBER),
        unit_cost=_convert(data, ("StandardPrice", "Price"), 0.0, float, "a number"),
        warehouse_location=_text(data, ("StorageLocation", "Plant")),
        metadata=_metadata(data, _SPARE_PART_PROPERTIES),
    )


def parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert SAP MaintenancePlan OData entity to a :class:`MaintenancePlan`.

    Raises:
        SapFieldError: If the cycle value is not a whole number, or a
            property a text field takes holds an object or a list.
    """
    cycle_val = _convert(data, ("MaintenancePlanCycleValue", "CycleValue"), 0, int, _WHOLE_NUMBER)
    cycle_unit = str(data.get("MaintenancePlanCycleUnit", data.get("CycleUnit", "DAY")))
    interval = _sap_cycle_to_interval(cycle_val, cycle_unit)

    return MaintenancePlan(
        id=_text(data, ("MaintenancePlan", "MaintenancePlanNumber")),
        asset_id=_text(data, ("Equipment",)),
        name=_text(data, ("MaintenancePlanDesc", "Description")),
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

    SAP system status is a compound string like ``"REL CNF PRC SETC"``.
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


_T = TypeVar("_T")

_WHOLE_NUMBER = "a whole number"


def _first(data: dict[str, Any], properties: tuple[str, ...], default: Any) -> tuple[str, Any]:
    """Return the first of ``properties`` the record holds and its value.

    The first property and ``default`` when it holds none of them.
    """
    for name in properties:
        if name in data:
            return name, data[name]
    return properties[0], default


def _text(data: dict[str, Any], properties: tuple[str, ...]) -> str:
    """Return the first of ``properties`` the record holds as text, ``""`` if none.

    A number or boolean becomes its text, as before. An object or a list is
    refused: as text it would carry whatever it holds into the entity (a
    deferred navigation link's ``uri``, say) instead of a value.

    Raises:
        SapFieldError: If the value is an object or a list.
    """
    name, value = _first(data, properties, "")
    if isinstance(value, dict | list):
        raise SapFieldError(name, "text")
    return str(value)


def _convert(
    data: dict[str, Any],
    properties: tuple[str, ...],
    default: Any,
    convert: Callable[[Any], _T],
    expected: str,
) -> _T:
    """Convert the first of ``properties`` the record holds, ``default`` if none.

    Raises:
        SapFieldError: If ``convert`` cannot convert the value, naming the
            property and ``expected``.
    """
    name, value = _first(data, properties, default)
    try:
        return convert(value)
    # int() or float() of text that is not a number, of a value that is not
    # text or a number, or int() of an infinite float.
    except (ValueError, TypeError, ArithmeticError):
        pass
    # Raised outside the except block: the conversion's message quotes the
    # value, and `from None` would still keep it as __context__.
    raise SapFieldError(name, expected)


def _date(data: dict[str, Any], properties: tuple[str, ...], default: datetime) -> datetime:
    """Parse the first of ``properties`` the record holds as a SAP date.

    ``default`` when it holds none of them or an empty value.

    Raises:
        SapFieldError: If :func:`parse_sap_datetime` cannot read the value.
    """
    name, value = _first(data, properties, None)
    if not value:
        return default
    try:
        return parse_sap_datetime(value)
    # A value that is not text has no startswith(); a /Date(...)/ that is not
    # an integer fails int(), and one out of range fails fromtimestamp(), with
    # OverflowError, ValueError or (on Windows) OSError.
    except (ValueError, TypeError, AttributeError, ArithmeticError, OSError):
        pass
    # Raised outside the except block, as in _convert.
    raise SapFieldError(name, "a date")


def _is_transport(name: str) -> bool:
    """Whether a member of an OData JSON object is transport, not a property.

    The OData V2 members in :data:`_TRANSPORT_MEMBERS`, and every OData V4
    annotation: a name with an ``@``, such as ``@odata.etag``, ``@odata.id``
    or a navigation property's ``to_Item@odata.navigationLink``. A property
    name cannot contain one.
    """
    return name in _TRANSPORT_MEMBERS or "@" in name


def _carries_transport(value: Any) -> bool:
    """Whether a value holds an OData JSON object with a transport member.

    A navigation property is one: deferred, its value is
    ``{"__deferred": {"uri": ...}}``; expanded, an entry or a ``results`` list
    of entries, each with its own ``__metadata``. Walked without recursion, so
    a deeply nested value cannot exhaust the stack.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(_is_transport(str(name)) for name in item):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _metadata(data: dict[str, Any], mapped: frozenset[str]) -> dict[str, Any]:
    """Return the record's other properties, for an entity's ``metadata``.

    Kept: each property not in ``mapped``, as SAP returned it. Left out: the
    OData transport members (see :func:`_is_transport`) and every value that
    carries one (see :func:`_carries_transport`), whose ``uri`` or link would
    take the SAP gateway's host and service path to the model and MCP
    clients. The record itself is not changed: :func:`order_etag` still reads
    the ETag from its ``__metadata``.
    """
    return {
        name: value
        for name, value in data.items()
        if name not in mapped and not _is_transport(name) and not _carries_transport(value)
    }
