# MCP Tools

Machina auto-registers MCP tools based on the capabilities declared by your
configured connectors. If a connector declares `READ_ASSETS`, the
`machina_list_assets` and `machina_get_asset` tools become available.

## Available Tools

### Read Tools

| Tool | Capability | Description |
|------|-----------|-------------|
| `machina_list_assets` | `READ_ASSETS` | List all assets in the plant registry |
| `machina_get_asset` | `READ_ASSETS` | Get details for a specific asset by ID |
| `machina_list_work_orders` | `READ_WORK_ORDERS` | List work orders, optionally filtered by asset or status |
| `machina_get_work_order` | `GET_WORK_ORDER` | Get a specific work order by ID |
| `machina_list_spare_parts` | `READ_SPARE_PARTS` | List spare parts, optionally filtered by asset |
| `machina_get_maintenance_plan` | `READ_MAINTENANCE_PLANS` | Get maintenance plans for an asset |
| `machina_search_manuals` | `SEARCH_DOCUMENTS` | Search equipment manuals and documentation (RAG) |
| `machina_get_sensor_reading` | `GET_LATEST_READING` | Get the latest sensor reading for an asset |
| `machina_get_alarms` | `SUBSCRIBE_TO_NODES` | Get active alarms |

### Write Tools

| Tool | Capability | Description |
|------|-----------|-------------|
| `machina_create_work_order` | `CREATE_WORK_ORDER` | Create a new work order |
| `machina_update_work_order` | `UPDATE_WORK_ORDER` | Update an existing work order |

## Sandbox Behavior

Write tools respect sandbox mode. When `sandbox: true` in config:

- The tool **does not** execute the write against the CMMS
- Instead, it returns a synthesized response showing what *would* have been written
- The response includes `"sandbox": true` in metadata
- Trace files record the attempted write

This is enforced at the connector boundary via the `@sandbox_aware` decorator —
it applies regardless of how the tool is called (MCP, agent runtime, or direct).

## Capability-Driven Registration

Tools are registered at startup based on what your connectors support:

```
Connector capabilities → CAPABILITY_TO_TOOL mapping → registered tools
```

If you configure only a CMMS connector (no IoT, no documents), only CMMS-related
tools appear. Add a `DocumentStoreConnector` and `machina_search_manuals` appears
automatically.

If a connector declares a capability but no matching tool exists, the server
raises an error at startup — this prevents silent capability gaps.

## Tool Signatures

All tools accept typed parameters (Pydantic models) and return JSON-serializable
results. Example:

```
machina_create_work_order(
    asset_id: str,          # Required: target asset
    description: str,       # Required: work description
    type: str = "corrective",  # corrective | preventive | predictive
    priority: str = "medium",  # emergency | high | medium | low
    failure_mode: str = "",    # Optional failure mode code
) → WorkOrder (JSON)
```

## Error Handling

Tool errors surface to the MCP client as `isError: true` with a text description.
Domain exceptions (e.g., `AssetNotFoundError`) are translated to human-readable
error messages — the raw Python traceback is not exposed.
