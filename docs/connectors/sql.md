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

Keep the DSN in the environment: it usually carries credentials. Errors never
echo it, and the connection log shows it redacted. Unknown settings keys are
refused with an error that names them.

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
ones. List-valued fields (the asset `failure_modes` column, and a failure-mode
catalog's `detection_methods`, `typical_indicators`, `recommended_actions`)
hold a semicolon-delimited string.

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

`create_work_order` is idempotent on the work-order ID: if the ID already
exists, the existing record is returned and nothing is inserted. It needs
`insert_table` and `insert_columns` on the `WorkOrder` mapping. Updating work
orders is not supported, and spare parts and maintenance history are not read
from SQL. With sandbox mode on, the insert never runs (`@sandbox_aware`).

## Use Cases

- **Legacy CMMS:** read an existing maintenance database alongside its own UI
- **In-house systems:** connect home-grown maintenance tables
- **Reporting databases:** read from data warehouses or replicas
