"""LLM tool definitions for function calling.

Provides utilities to auto-generate OpenAI-compatible tool schemas from
connector capabilities, and a registry of built-in maintenance tools.
"""

from __future__ import annotations

from typing import Any


def make_tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI function-calling tool definition.

    Args:
        name: Tool function name (e.g. ``"read_assets"``).
        description: Human-readable description of what the tool does.
        parameters: JSON Schema for the tool's parameters.

    Returns:
        A dict conforming to the OpenAI tools schema.
    """
    params = parameters or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }


# ---------------------------------------------------------------------------
# Built-in maintenance tools
# ---------------------------------------------------------------------------

SEARCH_ASSETS_TOOL = make_tool(
    name="search_assets",
    description=(
        "Search for assets (equipment) in the plant by name, location, type, or ID. "
        "Use this when the user asks about a specific piece of equipment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search query (asset name, ID, or location).",
            },
        },
        "required": ["query"],
    },
)

GET_ASSET_DETAILS_TOOL = make_tool(
    name="get_asset_details",
    description=(
        "Get full details for a specific asset by its ID, including metadata, "
        "criticality, failure modes, and hierarchy."
    ),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "The unique asset identifier (e.g. 'P-201').",
            },
        },
        "required": ["asset_id"],
    },
)

READ_WORK_ORDERS_TOOL = make_tool(
    name="read_work_orders",
    description=("Read work orders from the CMMS. Can filter by asset ID, status, or type."),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "Filter by asset ID (optional).",
            },
            "status": {
                "type": "string",
                "description": "Filter by status: created, assigned, in_progress, completed, closed.",
            },
        },
    },
)

CREATE_WORK_ORDER_TOOL = make_tool(
    name="create_work_order",
    description=("Create a new maintenance work order in the CMMS."),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "The asset this work order is for.",
            },
            "type": {
                "type": "string",
                "enum": ["corrective", "preventive", "predictive", "improvement"],
                "description": "Work order type.",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high", "emergency"],
                "description": "Priority level.",
            },
            "description": {
                "type": "string",
                "description": "Description of the maintenance task.",
            },
        },
        "required": ["asset_id", "type", "priority", "description"],
    },
)

SEARCH_DOCUMENTS_TOOL = make_tool(
    name="search_documents",
    description=(
        "Search maintenance manuals, procedures, and technical documents using "
        "semantic search. Returns relevant passages with source references."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The question or topic to search for in documents.",
            },
            "asset_id": {
                "type": "string",
                "description": "Optionally scope search to documents related to a specific asset.",
            },
        },
        "required": ["query"],
    },
)

CHECK_SPARE_PARTS_TOOL = make_tool(
    name="check_spare_parts",
    description=("Check spare part availability and inventory for a given asset or SKU."),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "Asset ID to look up compatible spare parts.",
            },
            "sku": {
                "type": "string",
                "description": "Specific spare part SKU to check.",
            },
        },
    },
)

DIAGNOSE_FAILURE_TOOL = make_tool(
    name="diagnose_failure",
    description=(
        "Diagnose probable failure modes for an asset based on symptoms, "
        "alarms, or technician observations. Returns ranked list of possible causes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "The asset experiencing issues.",
            },
            "symptoms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of observed symptoms (e.g. 'high vibration', 'noise').",
            },
        },
        "required": ["asset_id", "symptoms"],
    },
)

GET_MAINTENANCE_SCHEDULE_TOOL = make_tool(
    name="get_maintenance_schedule",
    description=("Get upcoming maintenance schedule for an asset or the entire plant."),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": "Filter schedule by asset ID (optional).",
            },
        },
    },
)


# All built-in tools, ready for the agent runtime
BUILTIN_TOOLS: list[dict[str, Any]] = [
    SEARCH_ASSETS_TOOL,
    GET_ASSET_DETAILS_TOOL,
    READ_WORK_ORDERS_TOOL,
    CREATE_WORK_ORDER_TOOL,
    SEARCH_DOCUMENTS_TOOL,
    CHECK_SPARE_PARTS_TOOL,
    DIAGNOSE_FAILURE_TOOL,
    GET_MAINTENANCE_SCHEDULE_TOOL,
]
