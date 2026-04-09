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

    connector = Maximo(
        url="https://maximo.example.com",
        auth=ApiKeyHeaderAuth(header_name="apikey", value="your-api-key"),
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
| `read_work_orders` | Read work orders (`mxwo` object structure) |
| `create_work_order` | Create new work orders |
| `read_spare_parts` | Read inventory items (`mxinventory` object structure) |
| `read_maintenance_plans` | Read PM triggers (`mxpm` object structure) |

## Usage Examples

### Read assets

```python
assets = await connector.read_assets()
for asset in assets:
    print(f"{asset.id}: {asset.name} (criticality: {asset.criticality})")
```

### Filter work orders

```python
wos = await connector.read_work_orders(
    asset_id="PUMP-201",
    status="INPRG",
)
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

## Entity Mapping

| Maximo Field | Machina Field |
|---|---|
| `assetnum` | `Asset.id` |
| `description` | `Asset.name` |
| `location` | `Asset.location` |
| `priority` (1-3) | `Asset.criticality` (A/B/C) |
| `wonum` | `WorkOrder.id` |
| `worktype` | `WorkOrder.type` (CM→Corrective, PM→Preventive, CP→Predictive, EV→Improvement) |
| `wopriority` | `WorkOrder.priority` (1→Emergency, 2→High, 3→Medium, 4→Low) |
| `status` | `WorkOrder.status` (WAPPR→Created, APPR→Assigned, INPRG→InProgress, COMP→Completed, …) |
| `itemnum` | `SparePart.sku` |
| `curbal` | `SparePart.stock_quantity` |
| `pmnum` | `MaintenancePlan.id` |
| `frequency` | `MaintenancePlan.interval.days` |

## Lean Mode

By default, `lean=True` adds `lean=1` to all API requests, which strips
OSLC namespace wrappers from responses. This produces cleaner, smaller
JSON payloads. Set `lean=False` if your Maximo instance requires full
OSLC-compliant responses.

## Known Limitations

- **Object structure customisation**: The connector targets standard Maximo object structures (`mxasset`, `mxwo`, `mxinventory`, `mxpm`). Custom object structures require subclassing.
- **Asset type**: Maximo does not expose a direct equipment-type field comparable to SAP's `EquipmentCategory`. All assets default to `ROTATING_EQUIPMENT`; use the `metadata` dict for Maximo-specific classification.
- **Pagination**: Uses Maximo's OSLC `responseInfo.nextPage` link-following. Very large result sets may benefit from server-side `oslc.where` filtering.

## API Reference

::: machina.connectors.cmms.maximo.MaximoConnector
