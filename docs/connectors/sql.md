# SQL Connector

Read maintenance data from a SQL database — a legacy CMMS, an in-house
maintenance database, a reporting replica — through ODBC or JDBC, with a
query-to-entity mapping in configuration.

## Install

```bash
pip install "machina-ai[sql]"        # ODBC, via pyodbc
pip install "machina-ai[sql-jdbc]"   # JDBC, via jaydebeapi + JPype1
```

You also need the database's own ODBC driver (or JDBC `.jar`) installed on
the host — for example Microsoft ODBC Driver 18 for SQL Server, IBM Db2 ODBC,
or psqlODBC for PostgreSQL.

## Configuration (YAML)

Connector type `sql` (alias `generic_sql`). Each entry under `tables` maps the
rows of a `SELECT` query to one domain entity:

```yaml
connectors:
  cmms:
    type: sql
    primary: true
    settings:
      dsn: "${MACHINA_SQL_DSN}"     # e.g. Driver={ODBC Driver 18 for SQL Server};Server=...;Database=...;UID=...;PWD=...
      capabilities: read_write      # default: read_only
      tables:
        assets:
          entity: Asset
          query: "SELECT * FROM EQUIPMENT"
          fields:
            id: {column: EQUIP_ID}
            name: {column: DESCRIPTION, coerce: strip}
            type: {column: EQUIP_CAT, enum_map: {POM: rotating_equipment, VAL: instrument}}
            criticality: {column: CRIT}
            failure_modes: {column: FAILURE_CODES}   # "BEAR-WEAR-01;SEAL-LEAK-01"
        work_orders:
          entity: WorkOrder
          query: "SELECT * FROM WORK_ORDERS"
          fields:
            id: {column: WO_ID}
            asset_id: {column: EQUIP_ID}
            description: {column: WO_DESC}
            status: {column: WO_STATUS, enum_map: {APERTO: created, CHIUSO: closed}}
          insert_table: WORK_ORDERS
          insert_columns: {id: WO_ID, asset_id: EQUIP_ID, description: WO_DESC}
```

Keep the DSN in the environment: it usually carries credentials. Connection
errors and the connection log show it redacted. Unknown top-level settings
keys are refused with an error that names them; a misspelled key inside a
table or field mapping is ignored, so check those by hand.

### Mapping reference

| Key | Where | Meaning |
|-----|-------|---------|
| `entity` | table | `Asset`, `WorkOrder` or `FailureMode` |
| `query` | table | The `SELECT` that returns the rows |
| `fields` | table | Entity field → `{column, coerce, enum_map, default}` |
| `insert_table`, `insert_columns` | `WorkOrder` table | Target of the parameterized `INSERT` used by `create_work_order` (identifiers are validated) |
| `coerce` | field | `strip`, `int`, `float`, `decimal`, `iso_date`, `db2_date`, `unix_ts`, `strip_ebcdic` |
| `enum_map` | field | Database value → domain value (e.g. a status or asset type) |
| `default` | field | Value used when the column is NULL |

Other settings: `driver_type` (`odbc` or `jdbc`; JDBC needs `jdbc_driver_class`
and usually `jdbc_driver_path`, the driver `.jar`), `ebcdic_codepage` (default `cp037`, for
`strip_ebcdic`), and `retry` (`max_retries`, `base_backoff`, `max_backoff`)
for transient read errors.

`connect()` opens the connection and runs each query to check that every
mapped column is present; a missing column fails with the list of available
ones. List-valued fields (the asset `failure_modes` column, a work order's
`requested_skills`, and a failure-mode catalog's `detection_methods`,
`typical_indicators`, `recommended_actions`) hold a semicolon-delimited
string. A work order's `failure_impact` is read case-insensitively
(`critical`, `degraded`, `incipient`; any other value reads as empty);
`spare_parts` cannot be mapped yet.

### Python

```python
from machina.connectors.sql import GenericSqlConnector

connector = GenericSqlConnector(
    dsn=dsn,
    tables={
        "assets": {
            "entity": "Asset",
            "query": "SELECT * FROM EQUIPMENT",
            "fields": {"id": {"column": "EQUIP_ID"}, "name": {"column": "DESCRIPTION"}},
        },
    },
)
await connector.connect()
assets = await connector.read_assets()
```

The keyword arguments are the same as the YAML `settings`; alternatively pass
a validated `SqlConnectorConfig` (from `machina.connectors.sql.schema`) as
`config=`.

## Capabilities

| Capability | Declared when | Description |
|-----------|---------------|-------------|
| `read_assets` | always | Rows of the `Asset` mapping (also `get_asset(id)`) |
| `read_work_orders` | always | Rows of the `WorkOrder` mapping, optionally filtered by `asset_id` / `status` |
| `create_work_order` | `capabilities: read_write` | Parameterized `INSERT` into `insert_table` |
| `read_failure_modes` | a `FailureMode` mapping is configured | The failure-mode catalog |

`create_work_order` needs `insert_table` and `insert_columns` on the
`WorkOrder` mapping, and `insert_columns` should include `id`, so the row is
stored under Machina's deterministic work-order ID. It is then idempotent on
that ID: before inserting, it runs the `WorkOrder` query and, if a row with
the same ID is there, returns that record and inserts nothing. The check and
the insert run under one lock inside the process; another process writing to
the same table can still race it, so give the ID column a unique constraint.
The check reads the whole `WorkOrder` query, so keep that query selective on a
large table.

Updating work orders is not supported, and spare parts and maintenance history
are not read from SQL. With sandbox mode on, neither the check nor the insert
runs (`@sandbox_aware`).

## Use Cases

- **Legacy CMMS:** read an existing maintenance database alongside its own UI
- **In-house systems:** connect home-grown maintenance tables
- **Reporting databases:** read from data warehouses or replicas
