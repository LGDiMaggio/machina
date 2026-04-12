# IBM Maximo Connector

The `MaximoConnector` integrates Machina with **IBM Maximo Manage** (EAM),
reading and creating maintenance data via the OSLC/JSON REST API.

## Prerequisites

- IBM Maximo Manage instance (7.6.0.2+ or Maximo Application Suite)
- API key (recommended), or Basic/MAXAUTH credentials
- Network access to the Maximo OSLC endpoints (`/maximo/oslc/os/`)

## Installation

```bash
pip install machina-ai[cmms-rest]
```

## Configuration

=== "Python (API Key)"

    ```python
    from machina.connectors import Maximo
    from machina.connectors.cmms import ApiKeyHeaderAuth
    from machina.domain.asset import AssetType

    connector = Maximo(
        url="https://maximo.example.com",
        auth=ApiKeyHeaderAuth(header_name="apikey", value="your-api-key"),
        asset_type_map={
            "PUMPS": AssetType.ROTATING_EQUIPMENT,
            "VESSELS": AssetType.STATIC_EQUIPMENT,
            "INSTRUMENTS": AssetType.INSTRUMENT,
        },
    )
    await connector.connect()
    ```

=== "Python (Basic Auth)"

    ```python
    from machina.connectors import Maximo
    from machina.connectors.cmms import BasicAuth

    connector = Maximo(
        url="https://maximo.example.com",
        auth=BasicAuth(username="maxadmin", password="secret"),
    )
    await connector.connect()
    ```

=== "YAML"

    ```yaml
    connectors:
      cmms:
        type: maximo
        url: https://maximo.example.com
        auth:
          type: api_key
          header_name: apikey
          value: ${MAXIMO_API_KEY}
    ```

## Capabilities

| Capability | Description |
|---|---|
| `read_assets` | Read asset records (`mxasset` object structure) |
| `read_work_orders` | Read work orders — filter by `asset_id` and/or `status` (accepts `WorkOrderStatus` enum or raw Maximo code) |
| `create_work_order` | Create new work orders |
| `update_work_order` | Update status, assignee, or description via PATCH |
| `read_spare_parts` | Read inventory items (`mxinventory` object structure) |
| `read_maintenance_plans` | Read PM triggers (`mxpm` object structure) |

### Convenience methods

These methods are available but are **not** declared as agent-discoverable capabilities:

| Method | Description |
|---|---|
| `get_work_order(wonum)` | Fetch a single work order by `wonum` |
| `close_work_order(wonum)` | Transition to CLOSED (Maximo `CLOSE`) via `update_work_order` |
| `cancel_work_order(wonum)` | Transition to CANCELLED (Maximo `CAN`) via `update_work_order` |

## Usage Examples

### Read assets

```python
assets = await connector.read_assets()
for asset in assets:
    print(f"{asset.id}: {asset.name} (criticality: {asset.criticality})")
```

### Filter work orders with Machina enum

```python
from machina.domain.work_order import WorkOrderStatus

wos = await connector.read_work_orders(
    asset_id="PUMP-201",
    status=WorkOrderStatus.IN_PROGRESS,  # auto-mapped to Maximo "INPRG"
)
```

### Get a single work order

```python
wo = await connector.get_work_order("WO-001")
if wo:
    print(f"{wo.id}: {wo.failure_mode} — {wo.failure_cause}")
```

### Create a work order

```python
from datetime import datetime, timezone
from machina.domain import WorkOrder, WorkOrderType, Priority

wo = WorkOrder(
    id="",
    type=WorkOrderType.CORRECTIVE,
    priority=Priority.HIGH,
    asset_id="PUMP-201",
    description="Replace mechanical seal",
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
    "WO-001",
    status=WorkOrderStatus.COMPLETED,
    assigned_to="john.doe",
)
await connector.close_work_order("WO-001")
```

## Asset Type Mapping

Maximo does not expose a direct equipment-type field. By default, all
assets are classified as `ROTATING_EQUIPMENT`. Provide an
`asset_type_map` to override based on your Maximo `classstructureid`
(or `assettype`) values:

```python
from machina.domain.asset import AssetType

connector = Maximo(
    url="https://maximo.example.com",
    auth=auth,
    asset_type_map={
        "PUMPS": AssetType.ROTATING_EQUIPMENT,
        "VESSELS": AssetType.STATIC_EQUIPMENT,
        "INSTRUMENTS": AssetType.INSTRUMENT,
        "MOTORS": AssetType.ELECTRICAL,
    },
)
```

Unmapped values fall back to `ROTATING_EQUIPMENT`.

## Entity Mapping

| Maximo Field | Machina Field |
|---|---|
| `assetnum` | `Asset.id` |
| `description` | `Asset.name` |
| `location` | `Asset.location` |
| `priority` (1-3) | `Asset.criticality` (A/B/C) |
| `classstructureid` | `Asset.type` (via `asset_type_map`) |
| `wonum` | `WorkOrder.id` |
| `worktype` | `WorkOrder.type` (CM→Corrective, PM→Preventive, CP→Predictive, EV→Improvement) |
| `wopriority` | `WorkOrder.priority` (1→Emergency, 2→High, 3→Medium, 4→Low) |
| `status` | `WorkOrder.status` (WAPPR→Created, APPR→Assigned, INPRG→InProgress, COMP→Completed, CLOSE→Closed, CAN→Cancelled) |
| `failurecode` | `WorkOrder.failure_mode` |
| `failureremark` / `problemcode` | `WorkOrder.failure_cause` |
| `itemnum` | `SparePart.sku` |
| `curbal` | `SparePart.stock_quantity` |
| `pmnum` | `MaintenancePlan.id` |
| `frequency` | `MaintenancePlan.interval.days` |

## Resilience

All HTTP calls route through a shared retry helper with exponential backoff.
See [SAP PM Connector — Resilience](sap-pm.md#resilience) for details.

## Known Limitations

- **Object structure customisation**: The connector targets standard Maximo object structures (`mxasset`, `mxwo`, `mxinventory`, `mxpm`). Custom object structures require subclassing.
- **Spare parts by asset**: Maximo's `mxinventory` does not directly link to assets. Filtering spare parts by `asset_id` is not supported; use work-order job plans instead.
- **Pagination**: Uses Maximo's OSLC `responseInfo.nextPage` link-following. Very large result sets may benefit from server-side `oslc.where` filtering.

## API Reference

::: machina.connectors.cmms.maximo.MaximoConnector
