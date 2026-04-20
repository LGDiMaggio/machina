"""Shared converters: coerced field dict → domain entity.

Used by Excel, SQL, and GenericCmms connectors to avoid duplicating
the dict-to-entity construction logic.
"""

from __future__ import annotations

from typing import Any

from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.work_order import (
    Priority,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)


def dict_to_asset(d: dict[str, Any]) -> Asset:
    """Build an Asset from a coerced field dict."""
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
        metadata={k: v for k, v in d.items() if k not in Asset.model_fields},
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
