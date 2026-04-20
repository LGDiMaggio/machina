# GenericCmms Connector

Connect to any REST-based CMMS using declarative YAML schema mapping.
No custom Python code required.

## Install

```bash
pip install "machina-ai[cmms-rest]"
```

## Operating Modes

The GenericCmms connector supports two modes:

| Mode | When to use | Data source |
|------|-------------|-------------|
| **REST** | Real CMMS with a REST API | HTTP endpoints |
| **Local** | Demos, testing, offline | JSON files in a directory |

## REST Mode

### Quick Start

```python
from machina.connectors.cmms import GenericCmmsConnector

connector = GenericCmmsConnector(
    url="https://cmms.example.com/api",
    auth=BearerAuth(token="..."),
)
```

### YAML Configuration

```yaml
connectors:
  cmms:
    type: generic_cmms
    primary: true
    settings:
      url: "${MACHINA_CMMS_URL}"
      auth:
        type: bearer
        token: "${MACHINA_CMMS_API_KEY}"
```

### Schema Mapping

Map your CMMS API responses to Machina domain entities:

```yaml
connectors:
  cmms:
    type: generic_cmms
    settings:
      url: "https://api.example.com"
      yaml_mapping_file: "config/cmms_mapping.yaml"
```

The mapping file defines how to translate API responses:

```yaml
entity_mappings:
  asset:
    endpoint: "/api/equipment"
    fields:
      id:
        source: "equipment_id"
        required: true
      name:
        source: "display_name"
      type:
        source: "category"
        coerce: "enum_map"
        enum_map:
          pump: "rotating_equipment"
          motor: "electrical"
          vessel: "static_equipment"
      criticality:
        source: "risk_level"
        coerce: "enum_map"
        enum_map:
          critical: "A"
          important: "B"
          standard: "C"

  work_order:
    endpoint: "/api/work-orders"
    fields:
      id:
        source: "wo_number"
        required: true
      asset_id:
        source: "equipment_id"
        required: true
      description:
        source: "wo_description"
```

### Field Mapping Options

Each field supports:

| Option | Type | Description |
|--------|------|-------------|
| `source` | string | JSONPath-lite path to the field (e.g., `"meta.name"`) |
| `coerce` | string | Type coercer: `"string"`, `"int"`, `"float"`, `"datetime"`, `"enum_map"`, `"regex_extract"` |
| `enum_map` | dict | Value translation table |
| `default` | any | Fallback when source is missing |
| `required` | bool | Skip row if missing (default: `false`) |
| `pattern` | string | Regex pattern (for `regex_extract` coercer) |

### Reverse Mapping (Writes)

To write back to the CMMS (e.g., create work orders), define reverse mappings:

```yaml
entity_mappings:
  work_order:
    endpoint: "/api/work-orders"
    create_endpoint:
      method: "POST"
      path: "/api/work-orders/create"
    fields: { ... }
    reverse_fields:
      asset_id:
        target: "equipment_id"
      priority:
        target: "priority_level"
        reverse_enum_map:
          high: "1"
          medium: "2"
          low: "3"
```

### Authentication

| Auth Type | Config |
|-----------|--------|
| Bearer token | `auth: { type: bearer, token: "..." }` |
| Basic auth | `auth: { type: basic, username: "...", password: "..." }` |
| API key header | `auth: { type: api_key, header: "X-API-Key", value: "..." }` |

### Pagination

| Strategy | Config |
|----------|--------|
| Offset/limit | `pagination: { type: offset_limit, limit: 100 }` |
| Cursor | `pagination: { type: cursor, cursor_field: "next_cursor" }` |
| Link header | `pagination: { type: link_header }` |

## Local Mode

Read from JSON files in a directory:

```python
connector = GenericCmmsConnector(data_dir="sample_data/cmms")
```

Expected files: `assets.json`, `work_orders.json`, `spare_parts.json`,
`maintenance_plans.json`, `failure_modes.json`.

## Capabilities

| Capability | REST | Local |
|-----------|------|-------|
| `READ_ASSETS` | Yes | Yes |
| `READ_WORK_ORDERS` | Yes | Yes |
| `GET_WORK_ORDER` | If endpoint configured | Yes |
| `CREATE_WORK_ORDER` | If endpoint configured | Yes |
| `UPDATE_WORK_ORDER` | If endpoint configured | Yes |
| `READ_SPARE_PARTS` | Yes | Yes |
| `READ_MAINTENANCE_PLANS` | If endpoint configured | Yes |
| `READ_MAINTENANCE_HISTORY` | Yes | Yes |

Capabilities are dynamic — they depend on which endpoints and files are available.
