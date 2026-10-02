# GenericCmms Connector

Connect to a CMMS that has a REST API — or to a folder of JSON files — and map
its records to Machina's domain entities in configuration. No custom Python
code required.

## Install

```bash
pip install "machina-ai[cmms-rest]"   # REST mode (httpx); local mode needs no extra
```

## Operating Modes

| Mode | Set | Data source |
|------|-----|-------------|
| **REST** | `url` | A CMMS REST API |
| **Local** | `data_dir` | JSON files in a directory (demos, tests, offline) |

## REST Mode

### The REST contract

The connector calls fixed paths under `url`:

| Operation | Request |
|-----------|---------|
| Connect (health check, must return 200) | `GET {url}/health` |
| Read assets | `GET {url}/assets`, `GET {url}/assets/{id}` |
| Read work orders | `GET {url}/work_orders?asset_id=…&status=…` |
| Create a work order | `POST {url}/work_orders` (JSON body, the created record in the response) |

List responses are a JSON array, unless a pagination strategy says otherwise
(its `items_path` unwraps `{"data": [...]}`-style bodies). Three more
operations are available when you configure their `endpoints` (paths relative
to `url`, `{id}` replaced by the work-order ID):

| `endpoints` key | Enables | Example |
|-----------------|---------|---------|
| `get_work_order` | `get_work_order` | `{path: "work_orders/{id}"}` |
| `update_work_order` | `update_work_order`, `close_work_order`, `cancel_work_order` | `{path: "work_orders/{id}", method: PATCH, field_map: {status: state}}` |
| `read_maintenance_plans` | `read_maintenance_plans` | `{path: "maintenance_plans"}` |

An update sends `{"status": …, "assigned_to": …, "description": …}` (only the
fields given, renamed through `field_map`) and re-reads the work order when
`get_work_order` is configured. Maintenance plans are read with an `interval`
of either a number of days or `{days, weeks, months, hours}`.

REST mode reads no spare parts and no maintenance history: those methods
return empty lists. Calls are single attempts, without retries.

### YAML Configuration

```yaml
connectors:
  cmms:
    type: generic_cmms
    primary: true
    settings:
      url: "${MACHINA_CMMS_URL}"
      api_key: "${MACHINA_CMMS_API_KEY}"     # bearer token
      endpoints:
        get_work_order: {path: "work_orders/{id}"}
        update_work_order: {path: "work_orders/{id}", method: PATCH}
        read_maintenance_plans: {path: "maintenance_plans"}
```

[`deploy/docker/`](../deployment/docker.md) runs this configuration against a
mock CMMS that implements the contract.

### Authentication

REST mode needs credentials: without `api_key` or `auth`, `connect()` raises
`ConnectorAuthError`. `api_key` is a shortcut for a bearer token; `auth`
selects a strategy by `type`:

| `auth` | Sends |
|--------|-------|
| `{type: bearer, token: "…"}` | `Authorization: Bearer …` |
| `{type: basic, username: "…", password: "…"}` | `Authorization: Basic …` |
| `{type: api_key, header_name: X-API-Key, value: "…"}` | `X-API-Key: …` (`header_name` defaults to `X-API-Key`) |
| `{type: none}` | nothing — for APIs without auth |

`auth` wins over `api_key` when both are set. Keep the values in environment
variables (`token: "${MACHINA_CMMS_TOKEN}"`). An invalid `auth` setting fails
with an error that names the problem, never the secret.

### Pagination

| `pagination` | Requests |
|--------------|----------|
| `{type: none}` (default) | One `GET`; optional `items_path` |
| `{type: offset_limit, limit_param: limit, offset_param: offset, page_size: 100}` | `?offset=…&limit=…` until a short page |
| `{type: page_number, page_param: page, size_param: per_page, page_size: 100, start_page: 1}` | `?page=…&per_page=…` until a short page |
| `{type: cursor, cursor_param: cursor, cursor_response_path: next_cursor, items_path: items}` | Follows the cursor until it is empty |

Every strategy accepts `items_path`, a JMESPath expression that extracts the
item list from a wrapped response (e.g. `data`); the cursor strategy defaults
it to `items`.

### Field Mapping

Without a mapping, the connector expects records already in Machina's field
names. To translate a different payload, give an inline `yaml_mapping` with an
`asset` and/or a `work_order` entry:

```yaml
    settings:
      url: "${MACHINA_CMMS_URL}"
      api_key: "${MACHINA_CMMS_API_KEY}"
      yaml_mapping:
        mapping:
          asset:
            endpoint: {path: "assets"}           # required by the schema, not used for requests
            fields:
              id: {source: equipment_id, required: true}
              name: {source: display_name, coerce: strip_whitespace}
              type:
                source: category
                coerce: enum_map
                enum_map: {pump: rotating_equipment, motor: electrical, vessel: static_equipment}
              criticality:
                source: risk_level
                coerce: enum_map
                enum_map: {critical: A, important: B, standard: C}
              metadata:
                manufacturer: {source: "mfg.name"}
          work_order:
            endpoint: {path: "work_orders"}
            fields:
              id: {source: wo_number, required: true}
              asset_id: {source: equipment_id, required: true}
              description: {source: wo_description}
            reverse_fields:                       # domain field → payload field, for creates
              asset_id: equipment_id
              priority:
                target: priority_level
                reverse_enum_map: {high: "1", medium: "2", low: "3"}
```

| Field option | Meaning |
|--------------|---------|
| `source` | Path into the record: `a.b.c`, with list indices like `items[0].x` |
| `coerce` | `int`, `float`, `float_it`, `bool_truthy`, `iso_date`, `iso_datetime`, `strip_whitespace`, `lowercase`, `regex_extract` (needs `pattern`), `enum_map` (needs `enum_map`); more can be registered through the `machina.coercers` entry point |
| `enum_map` | Value translation table |
| `default` | Value when the source is missing or null |
| `required` | Skip the record when the source is missing |

The request paths always come from the REST contract above: the mapping's
`endpoint`, `create_endpoint` and `root` entries are validated but not used to
build requests. For simple renames, the Python-only `schema_mapping` parameter
also accepts `{"assets": {"asset_id": "id"}}` or JMESPath extraction
(`{"assets": {"_fields": {"id": "equipment.id"}}}`).

### Python

```python
from machina.connectors.cmms import BasicAuth, GenericCmmsConnector, OffsetLimitPagination

connector = GenericCmmsConnector(
    url="https://cmms.example.com/api",
    auth=BasicAuth(username="svc", password=password),
    pagination=OffsetLimitPagination(page_size=50, items_path="data"),
)
await connector.connect()
assets = await connector.read_assets()
```

The YAML dict forms (`auth={"type": "basic", ...}`) work in Python too.

## Local Mode

Read from JSON files in a directory:

```python
connector = GenericCmmsConnector(data_dir="sample_data/cmms")
```

Files: `assets.json`, `work_orders.json`, `spare_parts.json`,
`maintenance_plans.json` and, optionally, `failure_modes.json`. Created and
updated work orders are written back to `work_orders.json` atomically (unless
a `yaml_mapping` is set, in which case the external file format is left
untouched and changes stay in memory). Creating a work order whose ID already
exists returns the existing record.

## Capabilities

| Capability | REST | Local |
|-----------|------|-------|
| `read_assets` | Yes | Yes |
| `read_work_orders` | Yes | Yes |
| `create_work_order` | Yes | Yes |
| `read_spare_parts` | Declared, but returns `[]` | Yes |
| `read_maintenance_history` | Declared, but returns `[]` | Yes (completed and closed work orders) |
| `get_work_order` | With the `get_work_order` endpoint | Yes |
| `update_work_order`, `close_work_order`, `cancel_work_order` | With the `update_work_order` endpoint | Yes |
| `read_maintenance_plans` | With the `read_maintenance_plans` endpoint | Yes |
| `read_failure_modes` | No | When `failure_modes.json` exists |

Capabilities are computed per instance, from the mode and the configured
endpoints. With sandbox mode on, creates and updates never run
(`@sandbox_aware`).
