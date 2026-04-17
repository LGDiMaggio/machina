"""MCP tool definitions — proof-of-life tool for Unit 3.

Full domain tool surface arrives in Unit 4.
"""

from __future__ import annotations

from typing import Any


async def machina_list_assets(ctx: Any) -> list[dict[str, Any]]:
    """List all assets from the configured CMMS.

    Returns a list of asset dictionaries with id, name, type,
    location, and criticality fields.
    """
    runtime: Any = ctx.request_context.lifespan_context["runtime"]
    try:
        cmms: Any = runtime.get_primary_cmms()
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
    except Exception:
        return []
