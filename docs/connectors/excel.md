# Excel / CSV Connector

Use Excel (`.xlsx`) and CSV files as a maintenance data source: an asset
registry, a work-order sheet the agent can append to, and an optional
failure-mode catalog. Column-to-field mapping lives in configuration — no
Python code required.

## Install

```bash
pip install "machina-ai[excel]"
```

The `excel` extra installs `openpyxl` (for `.xlsx`) and `watchdog` (for the
optional file watcher). CSV files need no extra.

## Configuration (YAML)

Connector type `excel` (alias `excel_csv`). Each sheet is described by a
schema: the file `path`, the `sheet` name (ignored for CSV), the `columns`
mapping, and — for the work-order sheet — a `write_mode` that makes it
writable.

```yaml
connectors:
  registry:
    type: excel
    primary: true
    settings:
      asset_registry:
        path: data/asset_registry.xlsx
        sheet: Assets
        columns:
          - {column: Code, field: id, required: true}
          - {column: Name, field: name, required: true}
          - {column: Type, field: type}
          - {column: Location, field: location}
          - {column: Criticality, field: criticality}
          - {column: Failure modes, field: failure_modes}   # "BEAR-WEAR-01;SEAL-LEAK-01"
      work_orders:
        path: data/workorders.xlsx
        sheet: Work orders
        write_mode: append
        columns:
          - {column: ID, field: id, required: true}
          - {column: Asset, field: asset_id, required: true}
          - {column: Type, field: type}
          - {column: Priority, field: priority}
          - {column: Status, field: status}
          - {column: Description, field: description}
          - {column: Created, field: created_at, coerce: datetime_parse}
```

At least one of `asset_registry`, `work_orders` and `failure_modes` must be
configured. Unknown settings keys are refused with an error that names them.
The [starter kit](../starter-kits/odl-generator-from-text.md) ships a complete,
runnable example.

### Column mapping

| Key | Default | Meaning |
|-----|---------|---------|
| `column` | (required) | Header text in the sheet |
| `field` | (required) | Target field of the domain entity (`Asset`, `WorkOrder`, `FailureMode`) |
| `type` | `str` | `str`, `int`, `float`, `date`, `datetime` or `bool` |
| `required` | `false` | A row with this cell empty is skipped (and logged) |
| `default` | `null` | Value used when the cell is empty |
| `coerce` | — | Named converter: `float_it`, `int_it`, `bool_it`, `date_parse`, `italian_date`, `datetime_parse`, `strip` |

`connect()` checks every configured header against the file and fails on a
missing column or an unknown `coerce` name. Multi-valued cells (the asset
`failure_modes` and `aliases` columns, and the failure-mode list fields
`detection_methods`, `typical_indicators`, `recommended_actions`) hold a
semicolon-delimited string, e.g. `"BEAR-WEAR-01;SEAL-LEAK-01"`. A sample
failure-mode catalog lives at `examples/sample_data/failure_modes.csv`.

### Python

```python
from machina.connectors.docs import ExcelCsvConnector

connector = ExcelCsvConnector(
    asset_registry={
        "path": "data/asset_registry.xlsx",
        "sheet": "Assets",
        "columns": [
            {"column": "Code", "field": "id", "required": True},
            {"column": "Name", "field": "name", "required": True},
        ],
    },
)
await connector.connect()
assets = await connector.read_assets()
```

The keyword arguments are the same as the YAML `settings`; alternatively pass
a validated `ExcelConnectorConfig` (from `machina.connectors.docs.excel_schema`)
as `config=`.

## Capabilities

| Capability | Declared when | Description |
|-----------|---------------|-------------|
| `read_assets` | always | Read asset rows (also `get_asset(id)`) |
| `read_work_orders` | always | Read work-order rows, optionally filtered by `asset_id` / `status` |
| `create_work_order` | `work_orders` has a `write_mode` | Append a work-order row |
| `update_work_order` | `work_orders` has a `write_mode` | Change status, assignee or description |
| `read_failure_modes` | a `failure_modes` sheet is configured | Read the failure-mode catalog |

Spare parts are not read from spreadsheets.

## Writes

- **Create** appends one row. It is idempotent on the work-order ID: creating
  a work order whose ID is already in the sheet returns the existing record
  instead of adding a duplicate row. A missing work-order file is created on
  the first write, with the header row.
- **Update** applies only legal status transitions (an illegal one, or an
  unknown status, raises `ConnectorError`), then rewrites the sheet from the
  in-memory cache to a temporary sibling and atomically replaces the file, so a
  crash mid-write cannot truncate it. If the write fails, the cached record is
  restored.
- A file open in another program raises `ConnectorLockedError`.
- `write_mode` accepts `append` or `overwrite`; either one makes the sheet
  writable — new work orders are always appended.

**Formula injection is neutralised on write.** A cell value that starts with
a spreadsheet formula trigger (`=`, `+`, `-`, `@`) is written with a leading
apostrophe, so Excel or LibreOffice treats it as text instead of executing it.
The guard is reversed on read, so values round-trip unchanged.

## Sandbox Mode

The write methods are guarded by `@sandbox_aware`: in sandbox mode nothing
reaches the file, and the agent and MCP tools answer with a marked sandbox
result instead.

## File Watcher

The connector reads its files on `connect()` and does not watch them by
itself. To pick up edits made while the agent runs, start a `FileWatcher`
with the connector's `refresh()` as callback; `refresh()` reloads every sheet
all-or-nothing, so a file caught mid-save leaves the previous data in place.

```python
from machina.connectors.docs.watcher import FileWatcher

watcher = FileWatcher(
    paths=["data/asset_registry.xlsx"],
    callback=connector.refresh,
    debounce_ms=500,          # fire once, after a save burst settles
    poll_fallback_sec=30,     # polling interval on SMB/CIFS shares
)
await watcher.start()
```

The `watcher:` settings block (`enabled`, `debounce_ms`, `poll_fallback_sec`)
is validated with the rest of the configuration, ready for your code to pass
on; the connector does not start a watcher from it.

## Use Cases

- **Quick demos:** load sample data from Excel without setting up a CMMS
- **Small teams:** use a spreadsheet as a lightweight CMMS
- **Starter kit:** the `odl-generator-from-text` template uses Excel as its
  default substrate
