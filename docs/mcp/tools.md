# MCP Tools

The server registers a tool only when a configured connector declares the
capability behind it. Configure a connector with `READ_ASSETS` and
`machina_list_assets` and `machina_get_asset` appear; leave out the document
store and `machina_search_manuals` stays hidden.

```
connector capabilities  →  CAPABILITY_TO_TOOL  →  registered tools
```

Each tool talks to one connector: CMMS tools to the primary CMMS (the
connector marked `primary: true`, otherwise the first one that reads assets),
the others to the first connector with the needed capability. Registration
looks at every configured connector, dispatch only at that one: a CMMS tool
turned on by a secondary CMMS still calls the primary, and fails when the
primary cannot serve it. The [capability matrix](../capabilities.md) shows
which connector declares what.

## Domain Tools

### Read Tools

| Tool | Capability | Parameters | Returns |
|------|-----------|------------|---------|
| `machina_list_assets` | `read_assets` | — | Assets (id, name, type, location, criticality) |
| `machina_get_asset` | `read_assets` | `asset_id` | One asset, with manufacturer, model, parent and failure modes |
| `machina_list_work_orders` | `read_work_orders` | `asset_id=""`, `status=""` | Work orders, optionally filtered |
| `machina_get_work_order` | `get_work_order` | `work_order_id` | One work order |
| `machina_get_maintenance_history` | `read_maintenance_history` | `asset_id` | Past work orders on the asset |
| `machina_list_spare_parts` | `read_spare_parts` | `asset_id=""` | Spare parts with stock, reorder point and unit cost |
| `machina_get_maintenance_plan` | `read_maintenance_plans` | — | All preventive-maintenance plans (id, asset, name, interval in days, tasks) |
| `machina_search_manuals` | `search_documents` | `query`, `top_k=5`, `asset_id=""`, `filters=None` | Matching document chunks with source, page, section and score |
| `machina_get_sensor_reading` | `get_latest_reading` | `asset_id` | Latest sensor reading |
| `machina_get_alarms` | `get_latest_reading` | `asset_id=""` | Active alarms |

`machina_search_manuals` returns document sources as bare file names, never
host paths. Its `filters` keys are `asset_id`, `doc_type`,
`equipment_class_code` and `section_title`.

!!! note "Sensor tools in v0.4"
    No connector type that a config file can create declares
    `get_latest_reading` (the OPC-UA and MQTT connectors declare
    subscription and node-read capabilities instead), so a config-built server
    does not offer the two sensor tools. No shipped connector implements
    `get_alarms()`: `machina_get_alarms` returns an error entry even where the
    tool is registered.

### Write Tools

| Tool | Capability | Parameters |
|------|-----------|------------|
| `machina_create_work_order` | `create_work_order` | `asset_id`, `description`, `priority="medium"`, `work_order_type="corrective"`, `failure_mode=""` |
| `machina_update_work_order` | `update_work_order` | `work_order_id`, `status=""`, `assigned_to=""`, `description=""` |
| `machina_close_work_order` | `close_work_order` | `work_order_id` |
| `machina_cancel_work_order` | `cancel_work_order` | `work_order_id`, `reason=""` |
| `machina_send_message` | `send_message` | `channel`, `text` |

`priority` is one of `emergency`, `high`, `medium`, `low`; `work_order_type`
is one of `corrective`, `preventive`, `predictive`, `improvement`; `status` in
`machina_update_work_order` is one of `created`, `assigned`, `in_progress`,
`completed`, `closed`, `cancelled`. `machina_create_work_order` checks that
the asset exists first, and derives the work-order ID from the content, so
repeating the same call yields the same ID; whether the connector uses that ID
to avoid a duplicate depends on the connector (see
[Uptime](../deployment/uptime.md#transient-failure-handling)). The `reason` of
`machina_cancel_work_order` is not sent to the CMMS — only the sandbox result
repeats it. `channel` in `machina_send_message` is the chat ID (Telegram), the
channel name (Slack) or the e-mail address (Email).

The `status` filter of `machina_list_work_orders` is passed to the connector
as given: the Generic CMMS, Excel/CSV and SQL connectors compare it with the
values above, while SAP PM, Maximo and UpKeep expect their own status codes
(for example `REL` on SAP PM).

The MCP server has no human-in-the-loop confirmation: a write tool runs when
the client calls it, and which calls happen is up to the MCP client and its
user. Keep the server in sandbox mode until you trust both.

Capabilities with no tool are not exposed over MCP: the calendar
capabilities, `read_failure_modes`, `receive_message`, `retrieve_section`,
`get_related_readings`, and the OPC-UA and MQTT capabilities
(`browse_nodes`, `read_node_value`, `read_node_values`, `subscribe_to_nodes`,
`subscribe_to_topics`, `publish_message`).

## Vendor Tools (opt-in)

Two non-portable escape hatches reach vendor APIs directly. They are
registered only with `enable_vendor_tools: true` under `mcp:` in the config;
outside sandbox mode they answer with an error entry when the matching
connector is not configured.

| Tool | Connector | Parameters |
|------|-----------|------------|
| `sap_pm_raw_iw38_notification` | SAP PM | `equipment_id`, `notification_type="M2"`, `description=""` |
| `maximo_raw_attribute_update` | Maximo | `resource_type`, `resource_id`, `attributes=None` |

`maximo_raw_attribute_update` patches one resource,
`/maximo/oslc/os/{resource_type}/{resource_id}`: `resource_type` must be an
object structure name (letters, digits and underscores), and `resource_id` —
the rest ID that ends the resource's `href` — is sent as one percent-encoded
path segment, with empty, `.` and `..` refused.

## Sandbox Behavior

With `sandbox: true` in the config, write tools do not reach the CMMS or the
messaging service. They return a synthesized result marked as such:

```json
{
  "id": "WO-SANDBOX-0000",
  "status": "created",
  "asset_id": "P-201",
  "description": "[SANDBOX — no real write performed] Replace bearing",
  "metadata": {"sandbox": true}
}
```

The write tools call the connector as usual; its `@sandbox_aware` guard
raises `SandboxViolationError` before anything is written, and the tool turns
that into the result above. The guard sits at the connector boundary, so it
also backs the agent runtime and workflows, and a direct Python call to a
connector write method in sandbox mode raises the exception instead of
returning a result. Reads still run: `machina_create_work_order` looks the
asset up in the CMMS before the guard stops the write. The vendor tools check
sandbox mode themselves before calling the vendor API.

## Error Handling

Missing records and missing connectors come back as an `error` entry in the
result (for example `{"error": "Asset 'P-999' not found"}`), so the client's
model can read them; so do an invalid `status` in `machina_update_work_order`,
an invalid `resource_type` or `resource_id` in `maximo_raw_attribute_update`,
an alarm source without `get_alarms()`, and connector failures in
`machina_list_assets`, `machina_get_maintenance_history` and
`machina_send_message`. Any other connector failure — and
`machina_create_work_order` on an unknown asset — raises: the client receives
an MCP error result carrying the exception message; the Python traceback is
not sent.
