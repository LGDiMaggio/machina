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
        api_key: ${UPKEEP_API_KEY}
    ```

## Capabilities

| Capability | Description |
|---|---|
| `read_assets` | Read all assets (`/api/v2/assets`) |
| `read_work_orders` | Read work orders — filter by `asset_id` and/or `status` (accepts `WorkOrderStatus` enum or raw UpKeep string) |
| `create_work_order` | Create a new work order |
| `update_work_order` | Update status, assignee, or description via PATCH |
| `read_spare_parts` | Read parts inventory (`/api/v2/parts`) — prefers `partNumber` / `barcode` as SKU |
| `read_maintenance_plans` | Read preventive-maintenance schedules (`/api/v2/preventive-maintenance`) |

### Convenience methods

These methods are available but are **not** declared as agent-discoverable capabilities:

| Method | Description |
|---|---|
| `get_work_order(id)` | Fetch a single work order by ID |
| `close_work_order(id)` | Transition to CLOSED (maps to UpKeep `complete`) via `update_work_order` |
| `cancel_work_order(id)` | Transition to CANCELLED (maps to UpKeep `on hold`) via `update_work_order` |

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
    status=WorkOrderStatus.IN_PROGRESS,  # auto-mapped to "in progress"
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
| `status` | `WorkOrder.status` (open→Created, in progress→InProgress, on hold→Assigned, complete→Completed) |
| `partNumber` / `barcode` / `id` | `SparePart.sku` (prefers physical identifier, falls back to record ID) |
| `name` (part) | `SparePart.name` |
| `quantity` | `SparePart.stock_quantity` |
| `frequencyDays` | `MaintenancePlan.interval.days` |

## Resilience

All HTTP calls route through a shared retry helper with exponential backoff.
See [SAP PM Connector — Resilience](sap-pm.md#resilience) for details.

## Known Limitations

- **Asset criticality**: UpKeep does not expose a native criticality field. All assets default to `Criticality.C`.
- **Work order types**: UpKeep uses `category` ("preventive" / "reactive"). The connector maps these to `PREVENTIVE` and `CORRECTIVE` respectively. Predictive and improvement types are not natively supported by UpKeep; for custom categories, subclass the connector.
- **Spare part filtering by asset**: The connector fetches all parts and filters client-side, since UpKeep's parts API does not support asset-level filtering. Filtering by `sku` is supported in-memory.
- **Failure data**: UpKeep has no standard failure-mode fields. Failure-related data may be available in `WorkOrder.metadata` depending on your UpKeep configuration.

## API Reference

::: machina.connectors.cmms.upkeep.UpKeepConnector
