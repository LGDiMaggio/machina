"""Shared converters: coerced field dict → domain entity.

Used by Excel, SQL, and GenericCmms connectors to avoid duplicating
the dict-to-entity construction logic.
"""

from __future__ import annotations

from typing import Any

from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.failure_mode import FailureMode
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)

#: Canonical delimiter for multi-valued cells/columns across substrates.
LIST_CELL_DELIMITER = ";"


def split_list_cell(value: Any) -> list[str]:
    """Split a delimited cell/column value into a clean list of strings.

    The committed multi-value encoding shared by the Excel and SQL
    substrates: a single string holding :data:`LIST_CELL_DELIMITER`
    (``;``)-delimited entries, e.g. ``"BEAR-WEAR-01;SEAL-LEAK-01"``.
    Whitespace around entries is stripped and empty entries (including
    ones produced by a trailing delimiter) are dropped.

    Args:
        value: Raw cell value — a delimited string, an existing
            list/tuple (passes through with per-entry stripping), or
            ``None``/empty (yields ``[]``).

    Returns:
        List of non-empty, whitespace-trimmed entries.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(LIST_CELL_DELIMITER) if part.strip()]


def dict_to_asset(d: dict[str, Any]) -> Asset:
    """Build an Asset from a coerced field dict.

    A ``failure_modes`` key carries the asset↔failure-code linkage and an
    ``aliases`` key the plant's local names for the asset: either a list or a
    single delimited string cell (see :func:`split_list_cell`).

    **Every model field must be passed explicitly here.** The ``metadata``
    catch-all keeps only keys that are *not* ``Asset`` fields, so promoting a
    key to a model field silently deletes it unless a line is added above —
    the value stops reaching ``metadata`` and never starts reaching the field.
    ``aliases`` was exactly this case: before it became a field it survived in
    ``metadata['aliases']``, and adding the field without this line would have
    been a pure regression. (``equipment_class_code`` is the live instance of
    the same bug — a model field this builder never passes, so it is dropped
    from every connector-sourced asset.)
    """
    return Asset(
        id=str(d.get("id", "")),
        name=str(d.get("name", "")),
        type=d.get("type", AssetType.ROTATING_EQUIPMENT),
        location=str(d.get("location", "")),
        manufacturer=str(d.get("manufacturer", "")),
        model=str(d.get("model", "")),
        serial_number=str(d.get("serial_number", "")),
        install_date=d.get("install_date"),
        criticality=d.get("criticality", Criticality.C),
        parent=d.get("parent"),
        failure_modes=split_list_cell(d.get("failure_modes")),
        aliases=split_list_cell(d.get("aliases")),
        metadata={k: v for k, v in d.items() if k not in Asset.model_fields},
    )


def dict_to_failure_mode(d: dict[str, Any]) -> FailureMode:
    """Build a FailureMode from a coerced field dict.

    List-valued fields (``detection_methods``, ``typical_indicators``,
    ``recommended_actions``) accept either a list or a single delimited
    string cell (see :func:`split_list_cell`). Scalar coercion (e.g.
    numeric strings for ``mtbf_hours``) is left to pydantic validation.
    """
    return FailureMode(
        # `or ""` so a NULL column raises the empty-code validator instead
        # of minting a literal 'None' catalog entry.
        code=str(d.get("code") or ""),
        name=str(d.get("name") or ""),
        mechanism=str(d.get("mechanism") or ""),
        category=str(d.get("category") or ""),
        detection_methods=split_list_cell(d.get("detection_methods")),
        typical_indicators=split_list_cell(d.get("typical_indicators")),
        recommended_actions=split_list_cell(d.get("recommended_actions")),
        mtbf_hours=d.get("mtbf_hours"),
        iso_14224_code=str(d["iso_14224_code"]) if d.get("iso_14224_code") else None,
    )


def dict_to_work_order(d: dict[str, Any]) -> WorkOrder:
    """Build a WorkOrder from a coerced field dict."""
    return WorkOrder(
        id=str(d.get("id", "")),
        type=d.get("type", WorkOrderType.CORRECTIVE),
        priority=d.get("priority", Priority.MEDIUM),
        status=d.get("status", WorkOrderStatus.CREATED),
        asset_id=str(d.get("asset_id", "")),
        description=str(d.get("description", "")),
        assigned_to=d.get("assigned_to"),
        estimated_duration_hours=d.get("estimated_duration_hours"),
        metadata={k: v for k, v in d.items() if k not in WorkOrder.model_fields},
    )
