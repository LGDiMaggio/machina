# MCP Tools

Machina auto-registers MCP tools based on the capabilities declared by your
configured connectors. If a connector declares `READ_ASSETS`, the
`machina_list_assets`, `machina_get_asset`, and `machina_diagnose_failure`
tools become available.

## Available Tools

### Read Tools

| Tool | Capability | Description |
|------|-----------|-------------|
| `machina_list_assets` | `READ_ASSETS` | List all assets in the plant registry |
| `machina_get_asset` | `READ_ASSETS` | Get details for a specific asset by ID |
| `machina_diagnose_failure` | `READ_ASSETS` | Rank probable failure modes for an asset from observed symptoms (see [Failure Diagnosis](#failure-diagnosis)) |
| `machina_list_work_orders` | `READ_WORK_ORDERS` | List work orders, optionally filtered by asset or status |
| `machina_get_work_order` | `GET_WORK_ORDER` | Get a specific work order by ID |
| `machina_list_spare_parts` | `READ_SPARE_PARTS` | List spare parts, optionally filtered by asset |
| `machina_get_maintenance_plan` | `READ_MAINTENANCE_PLANS` | Get maintenance plans for an asset |
| `machina_search_manuals` | `SEARCH_DOCUMENTS` | Search equipment manuals and documentation (RAG) |
| `machina_get_sensor_reading` | `GET_LATEST_READING` | Get the latest sensor reading for an asset |
| `machina_get_alarms` | `GET_LATEST_READING` | Get active alarms |

### Write Tools

| Tool | Capability | Description |
|------|-----------|-------------|
| `machina_create_work_order` | `CREATE_WORK_ORDER` | Create a new work order |
| `machina_update_work_order` | `UPDATE_WORK_ORDER` | Update an existing work order |

## Failure Diagnosis

`machina_diagnose_failure(asset_id, symptoms)` shares its ranking and notes
code with the agent's `diagnose_failure` tool. It looks the asset up on the
primary CMMS (the agent uses its plant registry), then:

- **Catalog:** harvested at call time from every connector declaring
  `READ_FAILURE_MODES`, then narrowed to the asset's declared `failure_modes`
  when it has any. The tool registers with `READ_ASSETS` alone — without a
  catalog it still answers, with a note saying none is configured.
- **Ranking:** symptoms match a mode's `typical_indicators` by shared tokens
  ("high vibration" matches `vibration_velocity_mm_s`), ranked by how many
  indicators matched, top 5. Each entry's `confidence` is the fraction of
  that mode's indicators that matched — not a probability.
- **Notes:** an empty `probable_failures` list always carries a `note` saying
  why — unknown asset, no catalog configured, declared modes missing from the
  catalog, or nothing matched (listing the indicators the catalog knows). A
  note can also accompany matches, e.g. when the asset declares no failure
  modes and the full catalog was searched.

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
