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

## How reads select attributes

Maximo answers a collection query with each member's `href` and nothing else unless the
query names the attributes it wants in `oslc.select`. Each read therefore selects the
attributes its mapping uses, plus `siteid`, which together with the record's number forms
its key:

| Read | Object structure | `oslc.select` |
|---|---|---|
| `read_assets`, `get_asset` | `mxasset` | `assetnum,siteid,description,location,manufacturer,serialnum,priority,parent,classstructureid,assettype` |
| `read_work_orders`, `get_work_order`, `read_maintenance_history` | `mxwo` | `wonum,siteid,description,status,wopriority,worktype,assetnum,lead,assignedownergroup,failurecode,problemcode,reportdate,changedate` |
| `read_spare_parts` | `mxinventory` | `itemnum,siteid,location,minlevel,item.description,invbalances{curbal},invcost{avgcost,lastcost}` |
| `read_maintenance_plans` | `mxpm` | `pmnum,siteid,assetnum,description,frequency,frequnit,status` |

- **Only documented attributes are named.** Each name is documented on the object
  structure's main object, a child object (`invbalances{…}`) or a related object
  (`item.description`), because Maximo may refuse a query that names an attribute the
  object does not have. ASSET has no model number, and the failure remark is a separate
  object, so `Asset.model` stays empty and `WorkOrder.failure_cause` comes from
  `problemcode`.
- **Null attributes are left out.** Maximo omits null attributes from its answer, so a
  missing attribute reads as the field's default: an empty string, no parent, zero
  stock, criticality C. A PM with no frequency unit gets no calendar interval rather
  than its frequency read as days.
- **Incomplete records end the read.** This covers a record without its number
  (`assetnum`, `wonum`, `itemnum` or `pmnum`), one whose number is not text or a whole
  number, and a record the mapping cannot read. A member that carries only its `href`, as
  on a page that lost the selection, is such a record. The read raises
  `ConnectorSchemaError` (a `ConnectorError`); no partial list is returned. A
  `maximo_member_refused` warning names the read, the object structure, the record's
  position in the read, the reason and, where they apply, the asset the read is for and
  the fields the domain model rejected. Neither the error nor the warning repeats a value
  from Maximo.
- **Negative balances are no stock.** A Maximo organization can allow negative current
  balances. A spare part whose bin balances add up to less than zero is reported with no
  stock, and one with a negative cost with no cost; Maximo's own figure is kept in
  `metadata` (`negative_balance`, `negative_unit_cost`). Quantities are whole units:
  fractions are dropped.
- **Next pages use Maximo's link.** Each next page is requested at the link Maximo
  returns, which repeats the query; the selection is not added again.
- **`metadata` holds the rest.** It keeps the selected attributes that no field takes,
  such as `siteid`, and the record's rest id as `rest_id`: the last segment of its URI,
  which `maximo_raw_attribute_update` takes as `resource_id`. It leaves out OSLC's own
  keys (`href`, `localref`, `_imagelibref` and `*_collectionref` name the host Maximo
  sees itself on, and `_rowstamp` is a locking token) and nested objects or lists, which
  no selection asks for and which, in a full resource read, are links.
- **Spare-part reads cost Maximo the most.** IBM's
  [Performance Tips](https://ibm-maximo-dev.github.io/maximo-restapi-documentation/performance/)
  note that a related attribute in dot notation, such as `item.description`, adds a
  database query for each record returned. On a large inventory, pass `sku` where you can.

The selections follow IBM's REST API guide
([Selecting](https://ibm-maximo-dev.github.io/maximo-restapi-documentation/query/selecting/))
and Maximo's data dictionary. They have not been checked against a live Maximo.

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
| `manufacturer` | `Asset.manufacturer` |
| `serialnum` | `Asset.serial_number` |
| `priority` (1-3) | `Asset.criticality` (A/B/C; no priority is C) |
| `parent` | `Asset.parent` |
| `classstructureid` (else `assettype`) | `Asset.type` (via `asset_type_map`) |
| `wonum` | `WorkOrder.id` |
| `worktype` | `WorkOrder.type` (CM→Corrective, PM→Preventive, CP→Predictive, EV→Improvement) |
| `wopriority` | `WorkOrder.priority` (1→Emergency, 2→High, 3→Medium, 4→Low) |
| `status` | `WorkOrder.status` (WAPPR→Created, APPR→Assigned, INPRG→InProgress, COMP→Completed, CLOSE→Closed, CAN→Cancelled) |
| `assetnum` | `WorkOrder.asset_id` |
| `lead` (else `assignedownergroup`) | `WorkOrder.assigned_to` |
| `failurecode` | `WorkOrder.failure_mode` |
| `problemcode` | `WorkOrder.failure_cause` |
| `reportdate` / `changedate` | `WorkOrder.created_at` / `WorkOrder.updated_at` |
| `itemnum` | `SparePart.sku` |
| `item.description` | `SparePart.name` |
| sum of `invbalances.curbal` (one row per bin or lot) | `SparePart.stock_quantity` (whole units; below zero is 0) |
| `minlevel` | `SparePart.reorder_point` |
| `invcost.avgcost` (else `lastcost`), first row | `SparePart.unit_cost` (below zero is 0) |
| `location` (the storeroom) | `SparePart.warehouse_location` |
| `pmnum` | `MaintenancePlan.id` |
| `assetnum` | `MaintenancePlan.asset_id` |
| `frequency` × `frequnit` | `MaintenancePlan.interval` (DAYS, WEEKS, MONTHS; YEARS as twelve months; no unit or another unit gives no interval) |
| `status` (`ACTIVE`) | `MaintenancePlan.active` |
| `siteid` | `metadata["siteid"]` (assets, work orders, spare parts) |
| last segment of `href` | `metadata["rest_id"]` (assets, work orders, spare parts) |

Selected attributes that no field takes stay in `metadata`. A spare part's `reorder` flag (yes/no, not a quantity) is one of them when Maximo sends it.

## Resilience

All HTTP calls route through a shared retry helper with exponential backoff.
See [SAP PM Connector — Resilience](sap-pm.md#resilience) for details.

## Known Limitations

- **Object structure customisation**: The connector targets standard Maximo object structures (`mxasset`, `mxwo`, `mxinventory`, `mxpm`). Custom object structures require subclassing.
- **Spare parts by asset**: Maximo's `mxinventory` does not directly link to assets. Filtering spare parts by `asset_id` is not supported; use work-order job plans instead.
- **Filter values**: asset and work-order numbers, SKUs and statuses are matched exactly through `oslc.where`. Maximo documents no escape inside its string literals, and its QBE framework reads wildcards (`%`, `*`, `?`), value lists (`,`, `[ ]`) and operators (`=`, `!`, `<`, `>`, `~`) inside them as query syntax. A value that contains any of `" \ % * ? , [ ] = ! < > ~` or a non-printable character, is blank, or is the keyword `null` is therefore refused with `ConnectorError` before any request, including the lookup `update_work_order` sends before writing. The message names the offending characters, not the value. A number is matched as its digits.
- **Work-order numbers never reach a URL path**: `update_work_order` matches the `wonum` in `oslc.where` and sends the update to the rest id Maximo returns (see [How updates are sent](#how-updates-are-sent)). That rest id is sent as one percent-encoded path segment (`/` becomes `%2F`), and one with an empty, `.` or `..` part between `/` or `\` separators is refused with `ConnectorError`, so a proxy that decodes `%2F` cannot resolve it into another resource.
- **The same `wonum` in several sites**: there is no site setting yet. An update refuses such a `wonum`; `get_work_order` returns the first match.
- **Not validated against a live Maximo**: the attribute selections (see [How reads select attributes](#how-reads-select-attributes)) and the response shapes the tests use follow IBM's REST API guide and Maximo's data dictionary. IBM does not document what Maximo does with a selected attribute that an object structure lacks (a customised one, say). If it refuses the query, the read fails with `ConnectorError` and the HTTP status.
- **Custom frequency units**: a PM with no `frequnit`, or one that is not DAYS, WEEKS, MONTHS or YEARS (a site-defined synonym, say), gets an empty interval, so it is reported as having no calendar due date. A month counts as 30 days in `Interval.total_days`, so a yearly PM's interval is 360 days.
- **Status changes**: a status update is sent as the plain `status` attribute. IBM's REST API guide shows status changes only through actions that call `changeStatus` and does not say whether `mxwo` applies the attribute directly; this has not been checked against a live Maximo. A rejected update raises `ConnectorError` with the HTTP status.
- **Pagination**: Follows the OSLC `responseInfo.nextPage.href` link of each page (a bare URL or a relative link is followed too). Each page is requested with the connector's credentials, so a link to another scheme, host or port than the configured `url`, or one with user info in it, raises `ConnectorError` and logs `maximo_next_page_refused` with the link's scheme and host: Maximo builds its links from its own configured address, so behind a proxy check that it returns the address Machina uses. A `nextPage` with no usable URL, a link the server already returned, more than 50,000 rows in one read, or more pages than those rows fill also raise `ConnectorError` instead of ending the read early or growing without bound. `get_asset` and `get_work_order` read only the first page: they use the first match. Very large result sets may benefit from server-side `oslc.where` filtering.

## API Reference

::: machina.connectors.cmms.maximo.MaximoConnector
