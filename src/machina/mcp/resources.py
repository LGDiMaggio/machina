"""MCP resources — versioned URI scheme for domain data.

Exposes Machina domain data as MCP resources so Claude Desktop and
other MCP clients can attach assets, work orders, and the failure
taxonomy to conversations.

URI scheme is **pre-stable in v0.3.0** — may change before v0.3.1.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from machina.runtime import MachinaRuntime

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Built-in failure taxonomy (served from memory, no connector required)
# ---------------------------------------------------------------------------

BUILTIN_FAILURE_TAXONOMY: list[dict[str, Any]] = [
    {
        "code": "BEAR-WEAR-01",
        "name": "Bearing Wear",
        "category": "mechanical",
        "mechanism": "fatigue",
        "detection_methods": ["vibration_analysis", "temperature_monitoring"],
    },
    {
        "code": "SEAL-LEAK-01",
        "name": "Seal Leakage",
        "category": "mechanical",
        "mechanism": "wear",
        "detection_methods": ["visual_inspection", "pressure_monitoring"],
    },
    {
        "code": "IMPE-EROS-01",
        "name": "Impeller Erosion",
        "category": "mechanical",
        "mechanism": "erosion",
        "detection_methods": ["vibration_analysis", "performance_monitoring"],
    },
    {
        "code": "MOTO-INSU-01",
        "name": "Motor Insulation Breakdown",
        "category": "electrical",
        "mechanism": "degradation",
        "detection_methods": ["insulation_resistance_test", "thermal_imaging"],
    },
    {
        "code": "CORR-PIPE-01",
        "name": "Pipe Corrosion",
        "category": "structural",
        "mechanism": "corrosion",
        "detection_methods": ["ultrasonic_thickness", "visual_inspection"],
    },
    {
        "code": "VALV-STICK-01",
        "name": "Valve Sticking",
        "category": "mechanical",
        "mechanism": "contamination",
        "detection_methods": ["stroke_test", "position_monitoring"],
    },
    {
        "code": "GEAR-TOOTH-01",
        "name": "Gear Tooth Damage",
        "category": "mechanical",
        "mechanism": "fatigue",
        "detection_methods": ["vibration_analysis", "oil_analysis"],
    },
    {
        "code": "BELT-WEAR-01",
        "name": "Belt Wear / Misalignment",
        "category": "mechanical",
        "mechanism": "wear",
        "detection_methods": ["vibration_analysis", "visual_inspection"],
    },
]


def _runtime_from_ctx(ctx: Any) -> MachinaRuntime:
    return ctx.request_context.lifespan_context["runtime"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Resource registration
# ---------------------------------------------------------------------------


def register_resources(server: Any) -> None:
    """Register all MCP resources on the server."""

    @server.resource(  # type: ignore[misc,untyped-decorator,unused-ignore]
        "machina://v1/assets/{asset_id}",
        name="machina_asset",
        title="Asset details",
        description="Read a single asset by ID from the configured CMMS.",
        mime_type="application/json",
    )
    async def read_asset(asset_id: str) -> str:
        ctx = server.get_context()
        runtime = _runtime_from_ctx(ctx)
        cmms = runtime.get_primary_cmms()
        asset = await cmms.get_asset(asset_id)  # type: ignore[attr-defined]
        if asset is None:
            return json.dumps({"error": f"Asset {asset_id!r} not found"})
        return asset.model_dump_json()  # type: ignore[no-any-return]

    @server.resource(  # type: ignore[misc,untyped-decorator,unused-ignore]
        "machina://v1/work-orders/{wo_id}",
        name="machina_work_order",
        title="Work order details",
        description="Read a single work order by ID from the configured CMMS.",
        mime_type="application/json",
    )
    async def read_work_order(wo_id: str) -> str:
        ctx = server.get_context()
        runtime = _runtime_from_ctx(ctx)
        cmms = runtime.get_primary_cmms()
        wo = await cmms.get_work_order(wo_id)  # type: ignore[attr-defined]
        if wo is None:
            return json.dumps({"error": f"Work order {wo_id!r} not found"})
        return wo.model_dump_json()  # type: ignore[no-any-return]

    @server.resource(  # type: ignore[misc,untyped-decorator,unused-ignore]
        "machina://v1/failure-taxonomy",
        name="machina_failure_taxonomy",
        title="Failure taxonomy",
        description=(
            "Built-in failure mode taxonomy. Served from memory — no connector required."
        ),
        mime_type="application/json",
    )
    async def read_failure_taxonomy() -> str:
        return json.dumps(BUILTIN_FAILURE_TAXONOMY, indent=2)

    logger.info("mcp_resources_registered", count=3)
