# SQL Connector

Read maintenance data from SQL databases (PostgreSQL, SQL Server, SQLite, DB2)
using YAML-based table-to-entity mapping.

## Install

```bash
pip install "machina-ai[cmms-rest]"
# Plus your database driver:
# pip install psycopg2-binary  # PostgreSQL
# pip install pyodbc            # SQL Server / DB2
```

## Quick Start

```python
from machina.connectors.cmms import GenericSqlConnector

connector = GenericSqlConnector(
    connection_string="postgresql://user:pass@host/db",
    table_mapping={
        "assets": {
            "table": "equipment",
            "fields": {
                "id": "equipment_id",
                "name": "display_name",
                "type": "equipment_type",
                "location": "install_location",
            },
        },
    },
)
await connector.connect()
assets = await connector.read_assets()
```

## Configuration (YAML)

```yaml
connectors:
  cmms:
    type: generic_sql
    primary: true
    settings:
      connection_string: "${DATABASE_URL}"
      table_mapping:
        assets:
          table: "equipment"
          fields:
            id: "equipment_id"
            name: "display_name"
            type: "equipment_type"
            location: "install_location"
            criticality: "risk_category"
        work_orders:
          table: "maintenance_orders"
          fields:
            id: "order_id"
            asset_id: "equipment_id"
            type: "order_type"
            priority: "priority_level"
            description: "order_description"
            status: "order_status"
```

## Table Mapping

Each entity type maps to a database table with field-level column mappings:

| Entity | Machina Field | Your Column |
|--------|--------------|-------------|
| Asset | `id` | `equipment_id` |
| Asset | `name` | `display_name` |
| WorkOrder | `asset_id` | `equipment_id` |
| WorkOrder | `priority` | `priority_level` |

The connector translates between your database schema and Machina's domain model
automatically.

## Capabilities

| Capability | Description |
|-----------|-------------|
| `READ_ASSETS` | Query asset table |
| `READ_WORK_ORDERS` | Query work order table |
| `CREATE_WORK_ORDER` | Insert work order row |
| `READ_SPARE_PARTS` | Query spare parts table (if mapped) |
| `READ_MAINTENANCE_HISTORY` | Query work orders filtered by asset |

## Supported Databases

| Database | Driver | Connection String |
|----------|--------|-------------------|
| PostgreSQL | `psycopg2` | `postgresql://user:pass@host/db` |
| SQL Server | `pyodbc` | `mssql+pyodbc://user:pass@host/db?driver=...` |
| SQLite | built-in | `sqlite:///path/to/file.db` |
| DB2 | `pyodbc` | `db2+pyodbc://user:pass@host/db` |

## Read-Only Mode

The SQL connector is read-only by default. Write support (for `CREATE_WORK_ORDER`)
requires explicit configuration:

```yaml
settings:
  connection_string: "${DATABASE_URL}"
  read_only: false  # Enable writes
```

## Use Cases

- **Legacy CMMS migration:** Read from an existing SQL database while evaluating Machina
- **Custom CMMS:** Connect to in-house maintenance databases
- **Reporting databases:** Read from data warehouses or replicas
