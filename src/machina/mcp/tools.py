"""MCP tool definitions — domain-level tools with capability-driven auto-registration.

Each tool is a plain async function with pydantic-typed params.  The
``CAPABILITY_TO_TOOL`` map drives auto-registration: ``build_server``
walks the runtime's connectors and registers only the tools whose
required capability is present.

Sandbox enforcement lives at the connector boundary (``@sandbox_aware``).
Write tools catch ``SandboxViolationError`` and return a synthesized
response with a sandbox disclaimer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import structlog

from machina.connectors.capabilities import Capability
from machina.exceptions import (
    AssetNotFoundError,
    ConnectorError,
    SandboxViolationError,
)

logger = structlog.get_logger(__name__)


def _runtime(ctx: Any) -> Any:
    return ctx.request_context.lifespan_context["runtime"]


# ---------------------------------------------------------------------------
# Read tools — CMMS
# ---------------------------------------------------------------------------


async def machina_list_assets(ctx: Any) -> list[dict[str, Any]]:
    """List all assets from the configured CMMS.

    Returns a list of asset dictionaries with id, name, type,
    location, and criticality fields.
    """
    runtime = _runtime(ctx)
    try:
        cmms = runtime.get_primary_cmms()
        assets = await cmms.read_assets()
        return [
            {
                "id": a.id,
                "name": a.name,
                "type": a.type.value if hasattr(a.type, "value") else str(a.type),
                "location": getattr(a, "location", ""),
                "criticality": a.criticality.value
                if hasattr(a.criticality, "value")
                else str(getattr(a, "criticality", "")),
            }
            for a in assets
        ]
    except ConnectorError as exc:
        return [{"error": str(exc)}]


async def machina_get_asset(ctx: Any, asset_id: str) -> dict[str, Any]:
    """Get a single asset by ID.

    Args:
        asset_id: The asset identifier to look up.
    """
    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()
    asset = await cmms.get_asset(asset_id)
    if asset is None:
        raise AssetNotFoundError(f"Asset {asset_id!r} not found")
    return {
        "id": asset.id,
        "name": asset.name,
        "type": asset.type.value if hasattr(asset.type, "value") else str(asset.type),
        "location": getattr(asset, "location", ""),
        "criticality": asset.criticality.value
        if hasattr(asset.criticality, "value")
        else str(getattr(asset, "criticality", "")),
        "manufacturer": getattr(asset, "manufacturer", ""),
        "model": getattr(asset, "model", ""),
        "parent": getattr(asset, "parent", None),
        "failure_modes": getattr(asset, "failure_modes", []),
    }


async def machina_list_work_orders(
    ctx: Any,
    asset_id: str = "",
    status: str = "",
) -> list[dict[str, Any]]:
    """List work orders, optionally filtered by asset or status.

    Args:
        asset_id: Filter by asset identifier.
        status: Filter by work order status.
    """
    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()
    kwargs: dict[str, Any] = {}
    if asset_id:
        kwargs["asset_id"] = asset_id
    if status:
        kwargs["status"] = status
    work_orders = await cmms.read_work_orders(**kwargs)
    return [
        {
            "id": wo.id,
            "type": wo.type.value,
            "priority": wo.priority.value,
            "status": wo.status.value,
            "asset_id": wo.asset_id,
            "description": wo.description,
            "assigned_to": wo.assigned_to,
        }
        for wo in work_orders
    ]


async def machina_get_work_order(ctx: Any, work_order_id: str) -> dict[str, Any]:
    """Get a single work order by ID.

    Args:
        work_order_id: The work order identifier.
    """
    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()
    wo = await cmms.get_work_order(work_order_id)
    if wo is None:
        return {"error": f"Work order {work_order_id!r} not found"}
    return {
        "id": wo.id,
        "type": wo.type.value,
        "priority": wo.priority.value,
        "status": wo.status.value,
        "asset_id": wo.asset_id,
        "description": wo.description,
        "assigned_to": wo.assigned_to,
        "failure_mode": wo.failure_mode,
        "created_at": wo.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Write tools — CMMS (sandbox-guarded at connector boundary)
# ---------------------------------------------------------------------------


async def machina_create_work_order(
    ctx: Any,
    asset_id: str,
    description: str,
    priority: str = "medium",
    work_order_type: str = "corrective",
) -> dict[str, Any]:
    """Create a new work order.

    Args:
        asset_id: Target asset identifier.
        description: Free-text description of the work.
        priority: Urgency level (emergency, high, medium, low).
        work_order_type: Type of maintenance (corrective, preventive, predictive, improvement).
    """
    from machina.domain.work_order import Priority, WorkOrder, WorkOrderType

    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()

    # Sandbox read-validation: verify asset exists before synthesizing
    asset = await cmms.get_asset(asset_id)
    if asset is None:
        raise AssetNotFoundError(
            f"Asset {asset_id!r} not found — cannot create work order for non-existent asset"
        )

    wo = WorkOrder(
        id="NEW",
        type=WorkOrderType(work_order_type),
        priority=Priority(priority),
        asset_id=asset_id,
        description=description,
    )

    try:
        result = await cmms.create_work_order(wo)
        return {
            "id": result.id,
            "type": result.type.value,
            "priority": result.priority.value,
            "status": result.status.value,
            "asset_id": result.asset_id,
            "description": result.description,
        }
    except SandboxViolationError:
        logger.info(
            "sandbox_write_blocked",
            operation="create_work_order",
            asset_id=asset_id,
        )
        return {
            "id": "WO-SANDBOX-0000",
            "type": work_order_type,
            "priority": priority,
            "status": "created",
            "asset_id": asset_id,
            "description": f"[SANDBOX — no real write performed] {description}",
            "metadata": {"sandbox": True},
        }


async def machina_update_work_order(
    ctx: Any,
    work_order_id: str,
    status: str = "",
    assigned_to: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Update an existing work order.

    Args:
        work_order_id: The work order to update.
        status: New status value.
        assigned_to: New assignee.
        description: New description.
    """
    from machina.domain.work_order import WorkOrderStatus

    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()

    kwargs: dict[str, Any] = {}
    if status:
        kwargs["status"] = WorkOrderStatus(status)
    if assigned_to:
        kwargs["assigned_to"] = assigned_to
    if description:
        kwargs["description"] = description

    try:
        result = await cmms.update_work_order(work_order_id, **kwargs)
        return {
            "id": result.id,
            "type": result.type.value,
            "priority": result.priority.value,
            "status": result.status.value,
            "asset_id": result.asset_id,
            "description": result.description,
            "assigned_to": result.assigned_to,
        }
    except SandboxViolationError:
        logger.info(
            "sandbox_write_blocked",
            operation="update_work_order",
            work_order_id=work_order_id,
        )
        return {
            "id": work_order_id,
            "description": "[SANDBOX — no real write performed] Update request logged.",
            "metadata": {"sandbox": True},
        }


# ---------------------------------------------------------------------------
# Read tools — spare parts, maintenance plans
# ---------------------------------------------------------------------------


async def machina_list_spare_parts(
    ctx: Any,
    asset_id: str = "",
) -> list[dict[str, Any]]:
    """List spare parts, optionally filtered by compatible asset.

    Args:
        asset_id: Filter by compatible asset.
    """
    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()
    kwargs: dict[str, Any] = {}
    if asset_id:
        kwargs["asset_id"] = asset_id
    parts = await cmms.read_spare_parts(**kwargs)
    return [
        {
            "sku": p.sku,
            "name": p.name,
            "stock_quantity": p.stock_quantity,
            "reorder_point": p.reorder_point,
            "unit_cost": p.unit_cost,
        }
        for p in parts
    ]


async def machina_get_maintenance_plan(ctx: Any) -> list[dict[str, Any]]:
    """List all preventive maintenance plans."""
    runtime = _runtime(ctx)
    cmms = runtime.get_primary_cmms()
    plans = await cmms.read_maintenance_plans()
    return [
        {
            "id": p.id,
            "asset_id": p.asset_id,
            "name": p.name,
            "interval_days": p.interval.total_days,
            "tasks": p.tasks,
        }
        for p in plans
    ]


# ---------------------------------------------------------------------------
# Read tools — documents
# ---------------------------------------------------------------------------


async def machina_search_manuals(
    ctx: Any,
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Search maintenance manuals and technical documentation.

    Args:
        query: The search query.
        top_k: Maximum number of results.
    """
    runtime = _runtime(ctx)
    matches = runtime.find_by_capability(Capability.SEARCH_DOCUMENTS)
    if not matches:
        return [{"error": "No document store connector configured"}]
    _, doc_store = matches[0]
    results = await doc_store.search_documents(query, top_k=top_k)
    return [
        {
            "source": getattr(r, "source", ""),
            "page": getattr(r, "page", 0),
            "content": getattr(r, "content", str(r)),
            "score": getattr(r, "score", 0.0),
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Read tools — IoT / sensors
# ---------------------------------------------------------------------------


async def machina_get_sensor_reading(
    ctx: Any,
    asset_id: str,
) -> dict[str, Any]:
    """Get the latest sensor reading for an asset.

    Args:
        asset_id: The asset identifier.
    """
    runtime = _runtime(ctx)
    matches = runtime.find_by_capability(Capability.GET_LATEST_READING)
    if not matches:
        return {"error": "No IoT connector configured"}
    _, iot = matches[0]
    return await iot.get_latest_reading(asset_id)  # type: ignore[no-any-return]


async def machina_get_alarms(
    ctx: Any,
    asset_id: str = "",
) -> list[dict[str, Any]]:
    """Get active alarms, optionally filtered by asset.

    Args:
        asset_id: Filter alarms by asset.
    """
    runtime = _runtime(ctx)
    matches = runtime.find_by_capability(Capability.GET_LATEST_READING)
    if not matches:
        return [{"error": "No IoT connector configured"}]
    _, iot = matches[0]
    if hasattr(iot, "get_alarms"):
        alarms = await iot.get_alarms(asset_id=asset_id)
        return [
            {
                "id": getattr(a, "id", ""),
                "asset_id": getattr(a, "asset_id", ""),
                "severity": getattr(a, "severity", "").value  # type: ignore[union-attr]
                if hasattr(getattr(a, "severity", ""), "value")
                else str(getattr(a, "severity", "")),
                "parameter": getattr(a, "parameter", ""),
                "value": getattr(a, "value", 0),
                "threshold": getattr(a, "threshold", 0),
            }
            for a in alarms
        ]
    return [{"info": "Alarm retrieval not supported by the configured IoT connector"}]


# ---------------------------------------------------------------------------
# Capability → Tool mapping for auto-registration
# ---------------------------------------------------------------------------

CAPABILITY_TO_TOOL: dict[Capability, list[Callable[..., Any]]] = {
    Capability.READ_ASSETS: [machina_list_assets, machina_get_asset],
    Capability.READ_WORK_ORDERS: [machina_list_work_orders],
    Capability.GET_WORK_ORDER: [machina_get_work_order],
    Capability.CREATE_WORK_ORDER: [machina_create_work_order],
    Capability.UPDATE_WORK_ORDER: [machina_update_work_order],
    Capability.READ_SPARE_PARTS: [machina_list_spare_parts],
    Capability.READ_MAINTENANCE_PLANS: [machina_get_maintenance_plan],
    Capability.SEARCH_DOCUMENTS: [machina_search_manuals],
    Capability.GET_LATEST_READING: [machina_get_sensor_reading, machina_get_alarms],
}


def get_tools_for_capabilities(
    capabilities: frozenset[Capability],
) -> list[Callable[..., Any]]:
    """Return the deduplicated list of tools matching the given capabilities."""
    seen: set[str] = set()
    tools: list[Callable[..., Any]] = []
    for cap in capabilities:
        for tool_fn in CAPABILITY_TO_TOOL.get(cap, []):
            if tool_fn.__name__ not in seen:
                seen.add(tool_fn.__name__)
                tools.append(tool_fn)
    return tools
