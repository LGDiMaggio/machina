# UpKeep Connector

The `UpKeepConnector` integrates Machina with **UpKeep**, a leading
cloud-based CMMS platform, via its REST API v2.

## Prerequisites

- UpKeep account with API access enabled
- API token (generated from **Account Settings → API Tokens** in the UpKeep web UI)

## Installation

```bash
pip install machina-ai[cmms-rest]
```

## Configuration

=== "Python"

    ```python
    from machina.connectors import UpKeep

    connector = UpKeep(api_key="your-upkeep-api-token")
    await connector.connect()
    ```

=== "YAML"

    ```yaml
    connectors:
      cmms:
        type: upkeep
        primary: true
        settings:
          api_key: ${UPKEEP_API_KEY}
    ```

## Capabilities

| Capability | Description |
|---|---|
| `read_assets` | Read all assets (`/api/v2/assets`) |
| `read_work_orders` | Read work orders — filter by `asset_id` and/or `status` (accepts `WorkOrderStatus` enum or raw UpKeep string) |
| `get_work_order` | Fetch a single work order by ID |
| `create_work_order` | Create a new work order |
| `update_work_order` | Update status, assignee, or description via PATCH |
| `read_spare_parts` | Read the parts inventory (`/api/v2/parts`), or with `asset_id` only the parts that asset lists (the asset, then each listed part); `sku` narrows either in memory — prefers `partNumber` / `barcode` as SKU |
| `read_maintenance_plans` | Read PM templates with their schedules (`/api/v2/pm`) — one plan per PM schedule |
| `read_maintenance_history` | Read an asset's completed work orders (`/api/v2/work-orders`, status `complete`) |

### Convenience methods

These methods are available but are **not** declared as agent-discoverable capabilities:

| Method | Description |
|---|---|
| `close_work_order(id)` | Transition to CLOSED (maps to UpKeep `complete`) via `update_work_order` |
| `cancel_work_order(id)` | Transition to CANCELLED (maps to UpKeep `onHold`) via `update_work_order` |

## Usage Examples

### Read assets

```python
assets = await connector.read_assets()
for asset in assets:
    print(f"{asset.id}: {asset.name}")
```

### Read work orders with Machina enum filter

```python
from machina.domain.work_order import WorkOrderStatus

wos = await connector.read_work_orders(
    asset_id="asset-123",
    status=WorkOrderStatus.IN_PROGRESS,  # auto-mapped to "inProgress"
)
```

### Get a single work order

```python
wo = await connector.get_work_order("wo-123")
```

### Create a work order

```python
from datetime import datetime, timezone
from machina.domain import WorkOrder, WorkOrderType, Priority

wo = WorkOrder(
    id="",
    type=WorkOrderType.CORRECTIVE,
    priority=Priority.HIGH,
    asset_id="asset-123",
    description="Replace worn bearing",
    created_at=datetime.now(tz=timezone.utc),
    updated_at=datetime.now(tz=timezone.utc),
)
created = await connector.create_work_order(wo)
print(f"Created: {created.id}")
```

### Update / close a work order

```python
from machina.domain.work_order import WorkOrderStatus

updated = await connector.update_work_order(
    "wo-123",
    status=WorkOrderStatus.COMPLETED,
    assigned_to="tech-user-id",
)
await connector.close_work_order("wo-123")
```

## Entity Mapping

| UpKeep Field | Machina Field |
|---|---|
| `id` | `Asset.id` |
| `name` | `Asset.name` |
| `category` | `Asset.type` (mapped to closest `AssetType`) |
| `location` | `Asset.location` |
| `make` | `Asset.manufacturer` |
| `model` | `Asset.model` |
| `serialNumber` | `Asset.serial_number` |
| `id` (work order) | `WorkOrder.id` |
| `title` | `WorkOrder.description` |
| `priority` (0-3) | `WorkOrder.priority` (0→Low, 1→Medium, 2→High, 3→Emergency) |
| `status` | `WorkOrder.status` (open→Created, inProgress→InProgress, onHold→Assigned, complete→Completed; filters and updates send the same values, with Closed as complete and Cancelled as onHold) |
| `partNumber` / `barcode` / `id` | `SparePart.sku` (prefers physical identifier, falls back to record ID) |
| `name` (part) | `SparePart.name` |
| `quantity` | `SparePart.stock_quantity` |
| `id` / `_id` (PM schedule) | `MaintenancePlan.id` |
| `asset` (PM schedule) | `MaintenancePlan.asset_id` |
| `repeatFrequency` × `repeatInterval` (PM schedule) | `MaintenancePlan.interval` (DAILY→days, WEEKLY→weeks, MONTHLY→months, YEARLY→12 months) |
| `endDate` / `scheduleHasEnded` (PM schedule) | `MaintenancePlan.active` (false once the end date has passed or UpKeep marks the schedule ended) |
| `name` (PM template) | `MaintenancePlan.name` |
| `tasks[].name` (PM template) | `MaintenancePlan.tasks` |
| `estimatedTime` (PM template, hours) | `MaintenancePlan.estimated_duration_hours` |

## Resilience

All HTTP calls route through a shared retry helper with exponential backoff.
See [SAP PM Connector — Resilience](sap-pm.md#resilience) for details.

## Known Limitations

- **Asset criticality**: UpKeep does not expose a native criticality field. All assets default to `Criticality.C`.
- **Work order types**: UpKeep uses `category` ("preventive" / "reactive"). The connector maps these to `PREVENTIVE` and `CORRECTIVE` respectively. Predictive and improvement types are not natively supported by UpKeep; for custom categories, subclass the connector.
- **Spare parts by asset**: `/api/v2/parts` has no asset filter, so `read_spare_parts(asset_id=...)` reads the asset (`GET /api/v2/assets/<ID>`), whose `parts` list holds the IDs of its parts, then fetches each listed part (`GET /api/v2/parts/<ID>`). That is 1 + k requests for k listed parts; the inventory is not paged. A part listed twice is fetched and returned once. An asset that lists no parts returns an empty list after the one request.
    - **Limit**: an asset listing more than 100 parts is refused with `ConnectorError` before any part request.
    - **Refusals**: the read returns all of the asset's parts or none. It raises `ConnectorError` when UpKeep has no such asset, or cannot return a part the asset lists (HTTP 404). It raises `ConnectorSchemaError` when UpKeep's answer is not a well-formed record of that asset (a record with another `id`, a body without one, or a malformed `parts` list), when a part record is malformed, or when a part request returns a different part. Only the asset's own record can say it lists no parts.
    - **SKU**: a `sku` narrows the asset's parts in memory, so both filters hold. Without `asset_id`, the connector pages `/api/v2/parts` and filters by `sku` in memory.
    - **Not validated live**: this follows UpKeep's documented API reference and has not been validated against a live UpKeep instance.
- **Record IDs in paths**: an asset, work-order or part ID is sent as one percent-encoded path segment (`/` becomes `%2F`). An empty ID, or one with an empty, `.` or `..` part between `/` or `\` separators, is refused before any request: with `ConnectorError` for an ID you pass, and with `ConnectorSchemaError`, refusing the whole read, for a part ID in an asset's `parts` list.
- **Failure data**: UpKeep has no standard failure-mode fields. Failure-related data may be available in `WorkOrder.metadata` depending on your UpKeep configuration.
- **Maintenance plans**: a PM template can schedule several assets, each with its own recurrence, so the connector returns one plan per schedule (soft-deleted templates are skipped). A meter-triggered schedule maps with an empty interval: the PM payload names the meter but not its unit. Legacy PM triggers (`/api/v2/preventive-maintenance`) are not read — their documented responses carry no asset, and PM templates reference them through `legacyPMId`.

## API Reference

::: machina.connectors.cmms.upkeep.UpKeepConnector
