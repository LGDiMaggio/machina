"""Maximo payload ↔ Machina domain entity mapping (pure functions).

Extracted from :mod:`machina.connectors.cmms.maximo` so the mapping
logic is testable on raw ``dict`` inputs — no HTTP mocks, no connector
state.

A Maximo collection query returns only each member's ``href`` unless it
names the attributes it wants in ``oslc.select`` (IBM's REST API guide,
"Selecting"). The ``*_SELECT`` constants below are those names, one per
object structure: the attributes each ``parse_*`` function takes from a
member, plus ``siteid``, which with the identity attribute
(:data:`IDENTITY_ATTRIBUTE`) forms the record's key. Only attributes
documented on the object structure's main object, its child objects
(``name{...}``) or a related object (``relation.attribute``) are named,
because Maximo may refuse a query that names an attribute the object does
not have. The parsers also read a few attributes that are not selected, for
records from other sources: ``modelnum``, ``failureremark``, and the older
flat inventory shape (``description``, ``curbal``, ``avgcost``,
``lastcost`` on the record itself).
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any
from urllib.parse import unquote, urlsplit

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
    "ASSET_SELECT",
    "IDENTITY_ATTRIBUTE",
    "INVENTORY_SELECT",
    "MAXIMO_PRIORITY_MAP",
    "MAXIMO_STATUS_MAP",
    "MAXIMO_WORKTYPE_MAP",
    "PM_SELECT",
    "REVERSE_MAXIMO_STATUS",
    "WORK_ORDER_SELECT",
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

# Maximo's PM frequency units (FREQUNIT): the Interval field each one counts
# and how many of that field one unit is. A year is twelve months.
_FREQUENCY_UNITS: dict[str, tuple[str, int]] = {
    "DAYS": ("days", 1),
    "WEEKS": ("weeks", 1),
    "MONTHS": ("months", 1),
    "YEARS": ("months", 12),
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
# oslc.select per object structure (see the module docstring)
# ---------------------------------------------------------------------------

# ASSET has no model-number attribute, so Asset.model is not selected.
ASSET_SELECT = (
    "assetnum,siteid,description,location,manufacturer,serialnum,priority,parent,"
    "classstructureid,assettype"
)
# The failure remark is a separate FAILUREREMARK object, not a WORKORDER
# attribute; WorkOrder.failure_cause comes from problemcode.
WORK_ORDER_SELECT = (
    "wonum,siteid,description,status,wopriority,worktype,assetnum,lead,assignedownergroup,"
    "failurecode,problemcode,reportdate,changedate"
)
# INVENTORY holds the reorder point (minlevel) but not the item's
# description (on the related ITEM), the balances (child INVBALANCES, one
# row per bin or lot) or the costs (child INVCOST, one row per condition
# code).
INVENTORY_SELECT = (
    "itemnum,siteid,location,minlevel,item.description,invbalances{curbal},"
    "invcost{avgcost,lastcost}"
)
PM_SELECT = "pmnum,siteid,assetnum,description,frequency,frequnit,status"

# The attribute that identifies a record of each object structure read here;
# a member without it is not a record (see the module docstring).
IDENTITY_ATTRIBUTE: dict[str, str] = {
    "mxasset": "assetnum",
    "mxwo": "wonum",
    "mxinventory": "itemnum",
    "mxpm": "pmnum",
}

# Keys OSLC adds to a member that describe the transport, not the record:
# URIs on the host Maximo sees itself on, often an internal name (``href``,
# ``localref``, ``_imagelibref``, ``<child>_collectionref``), and the
# optimistic-locking token ``_rowstamp``. They stay out of ``metadata``,
# which reaches the LLM and MCP clients.
_TRANSPORT_KEYS = frozenset({"href", "localref", "_imagelibref", "_rowstamp"})

# The separators a server or proxy may read in a decoded path segment.
_PATH_SEPARATORS = re.compile(r"[/\\]")
# The path a record's URI has before its rest id: ``.../os/<object structure>``.
_RESOURCE_COLLECTION = re.compile(r"/os/[A-Za-z0-9_]+\Z")


def _rest_id(href: object) -> str | None:
    """Return the rest id that ends a member's ``href``, or ``None``.

    Maximo addresses a record by this last segment of its URI. It is kept,
    decoded, without the URI's host. It is not kept when the ``href`` is not
    a record's URI (``.../os/<object structure>/<rest id>``), when the
    segment has no UTF-8 form (a non-UTF-8 escape, or a lone surrogate from
    a JSON escape), or when it could leave its path segment: an empty,
    ``.`` or ``..`` value, also before a ``;``, or one with ``/`` or ``\\``.
    """
    if not isinstance(href, str):
        return None
    try:
        collection, _, segment = urlsplit(href).path.rpartition("/")
        rest_id = unquote(segment, errors="strict")
        rest_id.encode("utf-8")
    except ValueError:  # an unparseable href ("http://[x"), or no UTF-8 form
        return None
    if (
        not _RESOURCE_COLLECTION.search(collection)
        or rest_id.split(";", 1)[0] in ("", ".", "..")
        or _PATH_SEPARATORS.search(rest_id)
    ):
        return None
    return rest_id


def _metadata(data: dict[str, Any], mapped: set[str]) -> dict[str, Any]:
    """Return the member's other attributes, for ``metadata``.

    Kept: the attributes no domain field took whose value is a plain value
    (text, number, boolean). Dropped: OSLC transport keys and the nested
    objects or lists no field took, which in a full resource read are links
    (``doclinks: {href: ...}``). The record's rest id is kept as
    ``rest_id``.
    """
    metadata = {
        k: v
        for k, v in data.items()
        if k not in mapped
        and k not in _TRANSPORT_KEYS
        and not k.endswith("_collectionref")
        and not isinstance(v, dict | list)
    }
    rest_id = _rest_id(data.get("href"))
    if rest_id is not None:
        metadata["rest_id"] = rest_id
    return metadata


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
        criticality=_maximo_criticality(data.get("priority")),
        parent=data.get("parent") or None,
        metadata=_metadata(
            data,
            {
                "assetnum",
                "description",
                "location",
                "manufacturer",
                "modelnum",
                "serialnum",
                "priority",
                "parent",
            },
        ),
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
        metadata=_metadata(
            data,
            {
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
            },
        ),
    )


def parse_spare_part(data: dict[str, Any]) -> SparePart:
    """Convert a Maximo MXINVENTORY JSON object to a :class:`SparePart`.

    The fields come from :data:`INVENTORY_SELECT`: the name from the related
    item's description, the stock from the sum of the ``invbalances`` rows
    (one per bin or lot), the reorder point from ``minlevel`` and the unit
    cost from the first ``invcost`` row (average cost, else last cost). A
    payload in the older flat shape (``description``, ``curbal``,
    ``avgcost``/``lastcost`` on the record itself) is still read. ``reorder``
    is Maximo's yes/no reorder flag, not a quantity, so it stays in
    ``metadata``.

    Quantities are whole units, rounded down. Nothing is adjusted to fit the
    domain model: a :class:`SparePart` holds no negative stock, cost or
    reorder point, so a record whose balances add up to less than zero (a
    Maximo organization can allow negative balances), or whose cost or
    ``minlevel`` is negative, fails its validation (``ValidationError`` on
    ``stock_quantity``, ``unit_cost`` or ``reorder_point``). A balance, cost
    or ``minlevel`` that is not a finite number is refused too
    (``OverflowError``, ``ValueError``, or ``decimal.InvalidOperation`` for
    balances that add up to infinity minus infinity).
    """
    item = data.get("item")
    name = item.get("description") if isinstance(item, dict) else None
    balances = data.get("invbalances")
    # Summed as decimals: as floats, 0.3, -0.1 and -0.2 add up to just below
    # zero, which rounds down to a negative stock Maximo does not hold. The
    # precision is fixed, so a host that lowers its decimal precision cannot
    # round a small negative total away.
    with localcontext(prec=34):
        if isinstance(balances, list):
            stock = sum((_decimal(row.get("curbal", 0)) for row in balances), Decimal(0))
        else:
            stock = _decimal(data.get("curbal", 0))
    costs = data.get("invcost")
    cost_row = costs[0] if isinstance(costs, list) and costs else data
    cost = float(cost_row.get("avgcost", cost_row.get("lastcost", 0.0)))
    if not math.isfinite(cost):  # an infinite balance fails floor() the same way
        raise ValueError("the unit cost is not a finite number")
    return SparePart(
        sku=str(data.get("itemnum", "")),
        name=str(name if name is not None else data.get("description", "")),
        # floor, not int(): -0.5 stays below zero instead of becoming 0.
        stock_quantity=math.floor(stock),
        reorder_point=math.floor(float(data.get("minlevel", 0))),
        unit_cost=cost,
        warehouse_location=str(data.get("location", "")),
        metadata=_metadata(
            data,
            {
                "itemnum",
                "description",
                "item",
                "invbalances",
                "curbal",
                "minlevel",
                "invcost",
                "avgcost",
                "lastcost",
                "location",
            },
        ),
    )


def parse_maintenance_plan(data: dict[str, Any]) -> MaintenancePlan:
    """Convert a Maximo MXPM JSON object to a :class:`MaintenancePlan`.

    ``frequency`` counts ``frequnit`` units: days, weeks, months or years
    (a year is twelve months). A plan with no ``frequnit`` (Maximo leaves a
    null one out of its answer) or with a unit outside those four
    (a site-defined synonym, say) gets an empty interval, so it is reported
    as having no calendar due date rather than a wrong one.
    """
    frequency = int(data.get("frequency", 0))
    unit = str(data.get("frequnit") or "").strip().upper()
    field, per_unit = _FREQUENCY_UNITS.get(unit, ("", 0))
    interval = Interval(**{field: frequency * per_unit}) if field else Interval()
    return MaintenancePlan(
        id=str(data.get("pmnum", "")),
        asset_id=str(data.get("assetnum", "")),
        name=str(data.get("description", "")),
        interval=interval,
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


def _decimal(value: Any) -> Decimal:
    """Read a Maximo number as the decimal it was written as (``0.1``, not 0.1000…0555)."""
    return Decimal(repr(float(value)))


def _maximo_criticality(priority_val: Any) -> Criticality:
    """Map Maximo numeric priority (1-3 → A/B/C); no priority is C."""
    try:
        val = int(priority_val)
    except (TypeError, ValueError):
        return Criticality.C
    if val <= 1:
        return Criticality.A
    if val <= 2:
        return Criticality.B
    return Criticality.C
