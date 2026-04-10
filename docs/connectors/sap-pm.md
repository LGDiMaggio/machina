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
| `read_work_orders` | Read maintenance orders — filter by `asset_id` and/or `status` (accepts `WorkOrderStatus` enum or raw SAP code) |
| `get_work_order` | Fetch a single maintenance order by number |
| `create_work_order` | Create maintenance orders (CSRF token handled automatically) |
| `update_work_order` | Update status, assignee, or description via PATCH (CSRF-safe) |
| `close_work_order` | Convenience wrapper: transition to CLOSED (SAP `TECO`) |
| `cancel_work_order` | Convenience wrapper: transition to CANCELLED (SAP `DLFL`) |
| `read_spare_parts` | Read BOM / material data (configurable endpoint, default `API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem`) |
| `read_maintenance_plans` | Read preventive-maintenance plans (`API_MAINTENANCEPLAN/MaintenancePlan`) |

## Usage Examples

### Read assets

```python
assets = await connector.read_assets()
for asset in assets:
    print(f"{asset.id}: {asset.name} ({asset.criticality})")
```

### Read work orders with Machina enum filter

```python
from machina.domain.work_order import WorkOrderStatus

wos = await connector.read_work_orders(
    asset_id="10000001",
    status=WorkOrderStatus.IN_PROGRESS,  # auto-mapped to SAP "PCNF"
)
```

### Get a single work order

```python
wo = await connector.get_work_order("4000001")
if wo:
    print(f"{wo.id}: {wo.status} — {wo.failure_mode}")
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

### Update / close a work order

```python
from machina.domain.work_order import WorkOrderStatus

# Update specific fields
updated = await connector.update_work_order(
    "4000001",
    status=WorkOrderStatus.COMPLETED,
    assigned_to="TECH_SMITH",
)

# Convenience wrappers
await connector.close_work_order("4000001")
await connector.cancel_work_order("4000002")
```

## Configurable BOM Endpoint

The spare-parts endpoint varies across SAP versions. The default targets
`API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem` (S/4HANA Cloud). Override
for on-premise or legacy systems:

```python
connector = SapPM(
    url="https://sap.example.com/sap/opu/odata/sap",
    auth=auth,
    bom_service="API_EQUIPMENT",           # legacy service
    bom_entity_set="EquipmentBOM",         # legacy entity set
    bom_material_field="Material",         # field name for SKU filter
    bom_equipment_field="Equipment",       # field name for asset filter
)
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
| `MaintenanceOrderType` | `WorkOrder.type` (PM01→Corrective, PM02→Preventive, PM03→Predictive, PM04→Improvement) |
| `MaintPriority` | `WorkOrder.priority` (1→Emergency, 2→High, 3→Medium, 4→Low) |
| `MaintenanceOrderSystemStatus` | `WorkOrder.status` (CRTD, REL, PCNF, CNF, TECO, CLSD, DLFL) |
| `MaintenanceActivityType` | `WorkOrder.failure_mode` |
| `MaintenanceCause` / `MaintNotifCause` | `WorkOrder.failure_cause` |

## Resilience

All HTTP calls route through a shared retry helper with exponential backoff.
Retries are triggered on:

- **429 Too Many Requests** — honours the `Retry-After` header
- **503 Service Unavailable** — transient upstream failures
- **Network errors** — `TimeoutException`, `ConnectError`, `ReadError`

Default: 3 retries, 0.5 s → 8 s backoff cap.

## Known Limitations

- **OData v2 vs v4**: The connector handles both response formats (`d.results` and `value`). Your SAP system may use either depending on the service version.
- **Custom fields**: SAP Z-fields are stored in `metadata` dict; access them via `asset.metadata["ZZ_CUSTOM_FIELD"]`.
- **CSRF tokens**: Write operations (create, update) automatically fetch a CSRF token within the same HTTP session to ensure cookie-based session affinity.
- **Functional locations**: Currently read as part of the `Asset.location` field. A dedicated functional-location hierarchy is planned for a future release.

## API Reference

::: machina.connectors.cmms.sap_pm.SapPmConnector
