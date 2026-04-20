# Excel / CSV Connector

Read and write maintenance data from Excel (`.xlsx`) and CSV files using
YAML-based schema mapping. No Python code required.

## Install

```bash
pip install "machina-ai[cmms-rest]"
```

## Quick Start

```python
from machina.connectors.cmms import ExcelCsvConnector

connector = ExcelCsvConnector(
    file_path="data/assets.xlsx",
    read_only=True,
)
await connector.connect()
assets = await connector.read_assets()
```

## Configuration (YAML)

```yaml
connectors:
  assets:
    type: excel_csv
    primary: true
    settings:
      file_path: "data/asset_registry.xlsx"
      read_only: true

  workorders:
    type: excel_csv
    settings:
      file_path: "data/workorders.xlsx"
      write_mode: append  # append | overwrite
```

## Schema Mapping

The connector uses YAML field mappings to translate between your spreadsheet
columns and Machina domain entities. See the
[GenericCmms YAML Mapping](generic_cmms.md) documentation for the full
`FieldSpec` syntax — the Excel connector uses the same mapping engine.

## Capabilities

| Capability | Mode | Description |
|-----------|------|-------------|
| `READ_ASSETS` | Read | Read asset rows from Excel/CSV |
| `READ_WORK_ORDERS` | Read | Read work order rows |
| `CREATE_WORK_ORDER` | Write | Append a new work order row |
| `READ_SPARE_PARTS` | Read | Read spare part rows |

## File Watcher

The connector supports file watching — it detects changes to the source file
and reloads data automatically. Enable in settings:

```yaml
settings:
  file_path: "data/assets.xlsx"
  watch: true
  poll_interval: 30  # seconds
```

## Sandbox Mode

When `sandbox: true`, write operations (`CREATE_WORK_ORDER`, etc.) are logged
but the file is not modified. The trace entry records what would have been written.

## Use Cases

- **Quick demos:** Load sample data from Excel without setting up a CMMS
- **Small teams:** Use Excel as a lightweight CMMS alternative
- **Data migration:** Read from Excel, write to a REST CMMS via GenericCmms
- **Starter-kit templates:** The `odl-generator-from-text` template uses Excel
  as its default substrate
