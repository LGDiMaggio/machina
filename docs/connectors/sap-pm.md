# SAP PM Connector

The `SapPmConnector` integrates Machina with **SAP Plant Maintenance** (SAP PM)
on SAP S/4HANA, reading and creating maintenance data via OData REST APIs.

## Prerequisites

- SAP S/4HANA system with OData services enabled
- API access to at least: `API_EQUIPMENT`, `API_MAINTENANCEORDER`, `API_MAINTENANCEPLAN`
- OAuth 2.0 Client Credentials (recommended) or HTTP Basic authentication

## Installation

```bash
pip install machina-ai[cmms-rest]
```

## Configuration

=== "Python"

    ```python
    from machina.connectors import SapPM
    from machina.connectors.cmms import OAuth2ClientCredentials

    connector = SapPM(
        url="https://sap.example.com/sap/opu/odata/sap",
        auth=OAuth2ClientCredentials(
            token_url="https://sap.example.com/oauth/token",
            client_id="my-client",
            client_secret="my-secret",
        ),
        sap_client="100",
    )
    await connector.connect()
    ```

=== "Python (Basic Auth)"

    ```python
    from machina.connectors import SapPM
    from machina.connectors.cmms import BasicAuth

    connector = SapPM(
        url="https://sap.example.com/sap/opu/odata/sap",
        auth=BasicAuth(username="SAP_USER", password="SAP_PASS"),
        sap_client="100",
    )
    await connector.connect()
    ```

=== "YAML"

    ```yaml
    connectors:
      cmms:
        type: sap_pm
        url: https://sap.example.com/sap/opu/odata/sap
        sap_client: "100"
        auth:
          type: oauth2_client_credentials
          token_url: https://sap.example.com/oauth/token
          client_id: ${SAP_CLIENT_ID}
          client_secret: ${SAP_CLIENT_SECRET}
    ```

## Capabilities

| Capability | Description |
|---|---|
| `read_assets` | Read equipment master records (`API_EQUIPMENT/Equipment`) |
| `read_work_orders` | Read maintenance orders (`API_MAINTENANCEORDER/MaintenanceOrder`) |
| `create_work_order` | Create maintenance orders (CSRF token handled automatically) |
| `read_spare_parts` | Read BOM / material data (`API_EQUIPMENT/EquipmentBOM`) |
| `read_maintenance_plans` | Read preventive-maintenance plans (`API_MAINTENANCEPLAN/MaintenancePlan`) |

## Usage Examples

### Read assets

```python
assets = await connector.read_assets()
for asset in assets:
    print(f"{asset.id}: {asset.name} ({asset.criticality})")
```

### Read work orders for an asset

```python
wos = await connector.read_work_orders(asset_id="10000001")
for wo in wos:
    print(f"{wo.id}: {wo.description} [{wo.status}]")
```

### Create a work order

```python
from datetime import datetime, timezone
from machina.domain import WorkOrder, WorkOrderType, Priority

wo = WorkOrder(
    id="",
    type=WorkOrderType.CORRECTIVE,
    priority=Priority.HIGH,
    asset_id="10000001",
    description="Replace bearing on drive end",
    created_at=datetime.now(tz=timezone.utc),
    updated_at=datetime.now(tz=timezone.utc),
)
created = await connector.create_work_order(wo)
print(f"Created: {created.id}")
```

## Entity Mapping

| SAP Field | Machina Field |
|---|---|
| `Equipment` | `Asset.id` |
| `EquipmentName` | `Asset.name` |
| `EquipmentCategory` | `Asset.type` (M→Rotating, E→Electrical, I→Instrument, …) |
| `FunctionalLocation` | `Asset.location` |
| `ABCIndicator` | `Asset.criticality` (A/B/C) |
| `MaintenanceOrder` | `WorkOrder.id` |
| `MaintenanceOrderType` | `WorkOrder.type` (PM01→Corrective, PM02→Preventive, …) |
| `MaintPriority` | `WorkOrder.priority` (1→Emergency, 2→High, 3→Medium, 4→Low) |
| `MaintenanceOrderSystemStatus` | `WorkOrder.status` (CRTD, REL, PCNF, CNF, TECO, CLSD) |

## Known Limitations

- **OData v2 vs v4**: The connector handles both response formats (`d.results` and `value`). Your SAP system may use either depending on the service version.
- **Custom fields**: SAP Z-fields are stored in `metadata` dict; access them via `asset.metadata["ZZ_CUSTOM_FIELD"]`.
- **CSRF tokens**: Write operations automatically fetch a CSRF token. If your SAP gateway has session affinity requirements, ensure your network allows the two-request pattern.
- **Functional locations**: Currently read as part of the `Asset.location` field. A dedicated functional-location hierarchy is planned for a future release.

## API Reference

::: machina.connectors.cmms.sap_pm.SapPmConnector
