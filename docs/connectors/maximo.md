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
        primary: true
        settings:
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
| `get_work_order` | Fetch a single work order by `wonum` |
| `create_work_order` | Create new work orders |
| `update_work_order` | Update status, assignee, or description — see [How updates are sent](#how-updates-are-sent) |
| `read_spare_parts` | Read inventory items (`mxinventory` object structure) |
| `read_maintenance_plans` | Read PM triggers (`mxpm` object structure) |
| `read_maintenance_history` | Read an asset's completed and closed work orders (`mxwo`, status `COMP` or `CLOSE`) |

### Convenience methods

These methods are available but are **not** declared as agent-discoverable capabilities:

| Method | Description |
|---|---|
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

## How updates are sent

Maximo does not address a record by its `wonum`. Each record has a URI whose last
segment is a *rest id* that the server derives from the record's primary key (`wonum`
and `siteid` for a work order), and IBM documents an update as a `POST` to that URI
with an `x-method-override: PATCH` header. So `update_work_order`, and
`close_work_order` / `cancel_work_order`, which go through it:

1. look the order up by `wonum`
   (`GET /maximo/oslc/os/mxwo?oslc.where=wonum="WO-001"&oslc.select=wonum,siteid&oslc.pageSize=2&lean=1`),
   following the answer's next pages until it holds two rows;
2. refuse, before writing anything, a `wonum` that matches no work order, matches
   work orders in more than one site (a `wonum` is unique only within a site), or
   whose one match does not name that `wonum` (compared without regard to case), as
   a server or proxy that dropped the filter would answer;
3. send `POST /maximo/oslc/os/mxwo/{rest id}` with `x-method-override: PATCH` and
   `patchtype: MERGE` (and `lean=1`, unless the connector was created with
   `lean=False`), and accept `204 No Content` or `200 OK`. Without
   `patchtype: MERGE`, Maximo deletes the child objects of a listed child collection
   that the request does not repeat;
4. read the order back from the same URI (`GET /maximo/oslc/os/mxwo/{rest id}`, with
   `lean=1` like the update) and return it.

Only the rest id is taken from the URI Maximo returns. Its host is the one Maximo sees
itself on (an internal name behind a proxy), so the update and the read-back go to the
configured `url`, like every other request. The rest id is sent as one percent-encoded
path segment; a URI that is not a work-order URI, a rest id with an escape that is not
UTF-8, or one that is empty or has an empty, `.` or `..` part between `/` or `\`
separators once decoded, is refused with `ConnectorError` before anything is written.
No error repeats the URI. Each refusal after the lookup is logged at WARNING as
`maximo_update_refused`, with the `wonum` and a `reason` (`not_found`,
`several_sites`, `other_work_order`, `no_work_order_uri` or `invalid_rest_id`).

The update `POST` is retried on `429` only. After a network error, a timeout or a
`503` it is not sent again, because the first request may have been applied. The
lookup and the read-back are reads and are retried like any other. If Maximo accepted
the update but the order cannot be read back, `ConnectorError` says that the update
was applied and must not be repeated, and `work_order_update_not_read_back` is logged
at WARNING with the error type.

The `maximo_raw_attribute_update` [MCP vendor tool](../mcp/tools.md#vendor-tools-opt-in)
writes through the same helper, so it sends the same `POST` to the rest id it is given.

Sources: the IBM Maximo REST API guide,
[Create and update](https://ibm-maximo-dev.github.io/maximo-restapi-documentation/crud/create_and_update)
and [Selecting](https://ibm-maximo-dev.github.io/maximo-restapi-documentation/query/selecting),
and `update()` / `merge()` in IBM's reference Java client,
[`MaximoConnector.java`](https://github.com/ibmmaximorestjsonapis/maximorestclient/blob/master/src/com/ibm/maximo/oslc/MaximoConnector.java).

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
- **Filter values**: asset and work-order numbers, SKUs and statuses are matched exactly through `oslc.where`. Maximo documents no escape inside its string literals, and its QBE framework reads wildcards (`%`, `*`, `?`), value lists (`,`, `[ ]`) and operators (`=`, `!`, `<`, `>`, `~`) inside them as query syntax. A value that contains any of `" \ % * ? , [ ] = ! < > ~` or a non-printable character, is blank, or is the keyword `null` is therefore refused with `ConnectorError` before any request, including the lookup `update_work_order` sends before writing. The message names the offending characters, not the value. A number is matched as its digits.
- **Work-order numbers never reach a URL path**: `update_work_order` matches the `wonum` in `oslc.where` and sends the update to the rest id Maximo returns (see [How updates are sent](#how-updates-are-sent)). That rest id is sent as one percent-encoded path segment (`/` becomes `%2F`), and one with an empty, `.` or `..` part between `/` or `\` separators is refused with `ConnectorError`, so a proxy that decodes `%2F` cannot resolve it into another resource.
- **The same `wonum` in several sites**: there is no site setting yet. An update refuses such a `wonum`; `get_work_order` returns the first match.
- **Attributes in collection reads**: the collection queries behind `read_assets`, `read_work_orders`, `get_work_order` and the other reads send no `oslc.select`. IBM's REST API guide shows such a query returning only each member's `href`; this has not been checked against a live Maximo. An update reads its order back from the order's own URI, which returns the record's attributes.
- **Status changes**: a status update is sent as the plain `status` attribute. IBM's REST API guide shows status changes only through actions that call `changeStatus` and does not say whether `mxwo` applies the attribute directly; this has not been checked against a live Maximo. A rejected update raises `ConnectorError` with the HTTP status.
- **Pagination**: Follows the OSLC `responseInfo.nextPage.href` link of each page (a bare URL or a relative link is followed too). Each page is requested with the connector's credentials, so a link to another scheme, host or port than the configured `url`, or one with user info in it, raises `ConnectorError` and logs `maximo_next_page_refused` with the link's scheme and host: Maximo builds its links from its own configured address, so behind a proxy check that it returns the address Machina uses. A `nextPage` with no usable URL, a link the server already returned, more than 50,000 rows in one read, or more pages than those rows fill also raise `ConnectorError` instead of ending the read early or growing without bound. `get_asset` and `get_work_order` read only the first page: they use the first match. Very large result sets may benefit from server-side `oslc.where` filtering.

## API Reference

::: machina.connectors.cmms.maximo.MaximoConnector
