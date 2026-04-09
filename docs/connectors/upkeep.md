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
| `read_work_orders` | Read work orders, filterable by asset and status (`/api/v2/work-orders`) |
| `create_work_order` | Create a new work order |
| `read_spare_parts` | Read parts inventory (`/api/v2/parts`) |
| `read_maintenance_plans` | Read preventive-maintenance schedules (`/api/v2/preventive-maintenance`) |

## Usage Examples

### Read assets

```python
assets = await connector.read_assets()
for asset in assets:
    print(f"{asset.id}: {asset.name}")
```

### Read work orders for an asset

```python
wos = await connector.read_work_orders(asset_id="asset-123")
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

### Read maintenance plans

```python
plans = await connector.read_maintenance_plans()
for plan in plans:
    print(f"{plan.name}: every {plan.interval.days} days")
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
| `priority` (1-4) | `WorkOrder.priority` (1→Low, 2→Medium, 3→High, 4→Emergency) |
| `status` | `WorkOrder.status` (open→Created, in progress→InProgress, complete→Completed) |
| `id` (part) | `SparePart.sku` |
| `name` (part) | `SparePart.name` |
| `quantity` | `SparePart.stock_quantity` |
| `frequencyDays` | `MaintenancePlan.interval.days` |

## Known Limitations

- **Asset criticality**: UpKeep does not expose a native criticality field. All assets default to `Criticality.C`. To set criticality, update the asset after import.
- **Work order types**: UpKeep uses `category` ("preventive" / "reactive"). The connector maps these to `WorkOrderType.PREVENTIVE` and `WorkOrderType.CORRECTIVE` respectively. Predictive and improvement types are not natively supported.
- **Spare part filtering by asset**: The connector fetches all parts and filters client-side, since UpKeep's parts API does not support asset-level filtering.

## API Reference

::: machina.connectors.cmms.upkeep.UpKeepConnector
