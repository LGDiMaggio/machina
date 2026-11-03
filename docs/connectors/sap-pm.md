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
        primary: true
        settings:
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
| `read_work_orders` | Read maintenance orders — `asset_id` is filtered server-side; `status` (a `WorkOrderStatus`, its value such as `closed` in any case, or a SAP system status code such as `REL`) is applied to the orders read (see [Work-order status](#work-order-status)) |
| `get_work_order` | Fetch a single maintenance order by number |
| `create_work_order` | Create maintenance orders (CSRF token handled automatically) |
| `update_work_order` | Update assignee or description via PATCH, and set `assigned`, `closed` or `cancelled` through the status's function import (CSRF-safe; see [Work-order status](#work-order-status)) |
| `read_spare_parts` | Read BOM / material data (configurable endpoint, default `API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem`). Filters by `sku`, and by `asset_id` only when `bom_equipment_field` is configured; otherwise an `asset_id` filter is refused with `ConnectorUnsupportedFilterError` |
| `read_maintenance_plans` | Read preventive-maintenance plans (`API_MAINTENANCEPLAN/MaintenancePlan`) |
| `read_maintenance_history` | Read an asset's confirmed, technically completed or closed maintenance orders (system status `CNF`, `TECO` or `CLSD`), selected from the orders `API_MAINTENANCEORDER/MaintenanceOrder` returns for its `Equipment` |

### Convenience methods

These methods are available but are **not** declared as agent-discoverable capabilities:

| Method | Description |
|---|---|
| `close_work_order(id)` | Transition to CLOSED — technical completion (`SetMaintOrdToTechCompleted`, SAP `TECO`) — via `update_work_order` |
| `cancel_work_order(id)` | Transition to CANCELLED — deletion flag (`SetMaintOrdStsToMrkdForDeltn`, SAP `DLFL`) — via `update_work_order` |

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
    status=WorkOrderStatus.IN_PROGRESS,  # orders whose system status maps to it (PCNF)
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

# Assign the order, then release it (ReleaseMaintenanceOrder → REL)
updated = await connector.update_work_order(
    "4000001",
    status=WorkOrderStatus.ASSIGNED,
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

`bom_equipment_field` is empty by default, because the default
`BillOfMaterialItem` entity has no equipment field. Until it names the entity
set's equipment field, `read_spare_parts` refuses any call with a non-empty
`asset_id`, with or without a `sku`: it raises `ConnectorUnsupportedFilterError`
(a `ConnectorError`) before sending a request. Reads by `sku` alone, and reads
with no filter, work without it. When `bom_equipment_field` is set, an
`asset_id` and a `sku` given together must both match. `connect()` logs one
`bom_equipment_field_unconfigured` warning while the field is unset.

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
| `SystemStatusText` | `WorkOrder.status` — the most advanced of DLFL, CLSD, TECO, CNF, PCNF, REL, CRTD on the line (see [Work-order status](#work-order-status)) |
| `MaintenanceActivityType` | `WorkOrder.failure_mode` |
| `MaintenanceCause` / `MaintNotifCause` | `WorkOrder.failure_cause` (empty for `API_MAINTENANCEORDER` orders, see below) |

`API_MAINTENANCEORDER`'s order has no `CreationDate`, `MaintenanceCause` or
`MaintNotifCause` property (none is in SAP's generated client for it). So
`WorkOrder.created_at` is the order's basic start date
(`MaintOrdBasicStartDate`), and `failure_cause` stays empty for orders read
from it.

## Work-order status

`API_MAINTENANCEORDER` has no property that holds one status. It lists an
order's active system statuses in `SystemStatusText`, for example
`CRTD MACM ORAP PRC`, and changes a status only through function imports. The
field holds at most 40 characters. When the active statuses do not fit, SAP
lists only those with the highest priority.

**Reading.** `WorkOrder.status` is the most advanced lifecycle status on the
line:

| System status | `WorkOrderStatus` |
|---|---|
| `DLFL` | cancelled |
| `CLSD` or `TECO` | closed |
| `CNF` | completed |
| `PCNF` | in progress |
| `REL` | assigned |
| `CRTD` | created |

`UserStatusText`, `MaintOrdProcessPhaseCode` and `MaintOrdProcessSubPhaseCode`
are kept in `WorkOrder.metadata`. An order closed with SAP's *Do Not Execute*
(`NCMP` with `CLSD`) reads as closed, not cancelled.

**Filtering.** `SystemStatusText` cannot be filtered on. On SAP S/4HANA Cloud
Public Edition, SAP answers such a query with "Query operation … not
implemented" ([KBA 3614100](https://userapps.support.sap.com/sap/support/knowledge/en/3614100)),
and the connector does not rely on another edition behaving otherwise. So
`read_work_orders` filters by `Equipment` on the server and applies `status` to
the orders it reads, and `read_maintenance_history` keeps the equipment's
orders whose status is completed or closed.

A system status code such as `REL` or `MANC` matches only when SAP lists it in
the 40 characters of `SystemStatusText`.

The KBA suggests filtering on `MaintOrdProcessPhaseCode` instead. The connector
does not, because process phases belong to SAP's phase model for maintenance
orders
([Maintenance Process Phases](https://help.sap.com/docs/SAP_S4HANA_CLOUD/2dfa044a255f49e89a3050daf3c61c11/57372b93c62943718032b05fe5551733.html)),
and an order outside it may carry no phase, which a phase filter would miss.

A `status` filter without `asset_id` reads every order, up to the 50,000-row
safety cap. On a large system, pass `asset_id` with it.

**Setting.** `update_work_order(status=...)` calls the function import for the
status, after the PATCH of `assigned_to` / `description` when those are given:

| `WorkOrderStatus` | Function import | SAP system status |
|---|---|---|
| `ASSIGNED` | `ReleaseMaintenanceOrder` | `REL` |
| `CLOSED` | `SetMaintOrdToTechCompleted` | `TECO` (technical completion; business completion, `CLSD`, is not set) |
| `CANCELLED` | `SetMaintOrdStsToMrkdForDeltn` | `DLFL` |

The connector reads the order just before the call.

- **Already set.** If the order already has the status, nothing is sent. A
  repeated close or cancel therefore succeeds instead of failing on SAP's
  refusal, and an order closed for business (`CLSD`) counts as closed.
- **ETag.** Otherwise SAP requires the order's ETag in an `If-Match` header.
  The ETag comes from the order's `LastChangeDateTime` as a read returns it.
  The connector takes it from the read's `__metadata.etag`, where the OData V2
  JSON format puts an entry's ETag. That SAP fills it there for this API has
  not been checked against a live system. An order without one is refused with
  `ConnectorError` before the call.
- **412.** SAP answers `412` when the order changed in between. The error says
  so, and the update can be retried.
- **Release first.** SAP sets `TECO` only on a released order. Otherwise it
  refuses the call, and the error carries the HTTP status.

The order is then read again, and the update succeeds only if the order has the
status. `SystemStatusText` holds only 40 characters, though. When the line is
full and does not list the system status that was set, the update cannot be
confirmed. `ConnectorError` then says so and asks for the order to be checked
in SAP; the warning `work_order_status_unconfirmed` is logged. When the line has
room and still lacks it, the error says the status change was not applied.

If the PATCH of `assigned_to` / `description` was sent before a status step
that fails, the error says the PATCH was applied. If the order cannot be read
back after the update, the error says the update was applied and must not be
repeated.

SAP sets `CRTD` on create, and `PCNF` / `CNF` from time confirmations. So
`CREATED`, `IN_PROGRESS` and `COMPLETED` are refused with `ConnectorError`
before any request.

Property names, function imports and their parameters are taken from:

- the documentation of the API on the SAP Help Portal, for SAP S/4HANA Cloud
  Public Edition 2608 and SAP S/4HANA 2025 FPS02;
- SAP's generated client for the service
  (`@sap/cloud-sdk-vdm-maintenance-order-service` 2.1.0).

They have not been tested against a live system.

SAP has deprecated `API_MAINTENANCEORDER`, in SAP S/4HANA Cloud 2308 and
SAP S/4HANA 2023, in favour of Maintenance Order (Version 2),
`API_MAINTENANCEORDER_0002`. The connector does not use that service yet.

## Resilience

All HTTP calls route through a shared retry helper with exponential backoff.
Retries are triggered on:

- **429 Too Many Requests** — for every method, since the server refused the
  request without processing it
- **503 Service Unavailable** — for idempotent methods only, since a gateway
  can answer 503 after the backend applied a POST or PATCH
- **Network errors** — `TimeoutException`, `ConnectError`, `ReadError`, for
  idempotent methods only

Default: 3 retries, 0.5 s → 8 s backoff cap. A `Retry-After` of up to 8 s on a
429 or 503 replaces the computed delay; a longer one fails the call at once
instead of retrying inside the server's window. See
[Uptime and Resilience](../deployment/uptime.md) for what that means for
paged reads and startup.

## Known Limitations

- **OData v2 vs v4**: The connector handles both response formats (`d.results` and `value`). Your SAP system may use either depending on the service version.
- **Custom fields**: SAP Z-fields are stored in `metadata` dict; access them via `asset.metadata["ZZ_CUSTOM_FIELD"]`.
- **CSRF tokens**: Write operations (create, update) automatically fetch a CSRF token within the same HTTP session to ensure cookie-based session affinity.
- **Status filters**: applied to the orders read, not on the server (see [Work-order status](#work-order-status)). Combine `status` with `asset_id` on a large system.
- **Functional locations**: Currently read as part of the `Asset.location` field. A dedicated functional-location hierarchy is planned for a future release.
- **Spare parts by asset**: `read_spare_parts` can filter by `asset_id` only through `bom_equipment_field` (see [Configurable BOM Endpoint](#configurable-bom-endpoint)). While it is unset (the default), a non-empty `asset_id` raises `ConnectorUnsupportedFilterError` before any request, even when a `sku` is also given. The connector never answers `[]` for an asset it could not filter, and never drops the asset filter to return the whole BOM or a bare `sku` match. Look the part up by `sku` without an asset, or configure `bom_equipment_field`.
- **Filter values and keys**: equipment, order and material numbers and statuses reach `$filter` as OData string literals with any `'` doubled, so a value cannot close its literal and add a clause; a number is sent as its digits. The `MaintenanceOrder(...)` key of `update_work_order` is also percent-encoded (`/`, `?`, `#`, `%`, `(`, `)`), so it stays one path segment, and a key that is empty or has an empty, `.` or `..` part between `/` or `\` separators is refused with `ConnectorError` before any request.

## API Reference

::: machina.connectors.cmms.sap_pm.SapPmConnector
