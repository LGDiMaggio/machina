# MCP Resources

Machina exposes plant data as MCP resources under a versioned URI scheme,
`machina://v1/...`. `v1` is a stable contract: a breaking change to a
resource's shape goes to a new version segment, never into `v1`.

All four resources are registered whatever the connector set. The two that
read from the CMMS use the primary CMMS and fail when none is configured; the
other two are served from memory.

| URI | Source | Contents |
|-----|--------|----------|
| `machina://v1/assets/{asset_id}` | primary CMMS | One asset |
| `machina://v1/work-orders/{wo_id}` | primary CMMS | One work order |
| `machina://v1/failure-taxonomy` | memory | Built-in failure-mode reference list |
| `machina://v1/capabilities` | memory | Code-derived self-description of the framework |

The two templated URIs are listed by MCP clients as resource templates, the
two fixed ones as resources. Every resource returns `application/json`.

## Asset Details

**URI:** `machina://v1/assets/{asset_id}`

The full `Asset` record as stored in the CMMS:

```json
{
  "id": "P-201",
  "name": "Centrifugal Pump",
  "type": "rotating_equipment",
  "location": "Building A",
  "manufacturer": "Grundfos",
  "model": "CR 32-2",
  "serial_number": "",
  "install_date": null,
  "criticality": "A",
  "parent": null,
  "children": [],
  "failure_modes": ["BEAR-WEAR-01", "SEAL-LEAK-01"],
  "aliases": [],
  "metadata": {},
  "equipment_class_code": null
}
```

An unknown ID returns `{"error": "Asset 'P-999' not found"}`.

## Work Order Details

**URI:** `machina://v1/work-orders/{wo_id}`

The full `WorkOrder` record: ID, type, priority, status, asset ID,
description, assignee, failure mode and timestamps. An unknown ID returns an
`error` entry, as for assets.

## Failure Taxonomy

**URI:** `machina://v1/failure-taxonomy`

A built-in reference list of eight common failure modes, each with a code,
category, mechanism and detection methods. It does not read the failure modes
your CMMS defines.

```json
[
  {
    "code": "BEAR-WEAR-01",
    "name": "Bearing Wear",
    "category": "mechanical",
    "mechanism": "fatigue",
    "detection_methods": ["vibration_analysis", "temperature_monitoring"]
  }
]
```

## Capabilities

**URI:** `machina://v1/capabilities`

The framework's self-description: connector types × capabilities, extension
seams and the shape of the config schema. It is generated from the code,
carries no configured values, and is byte-identical to
`machina describe --json` and the published [capability matrix](../capabilities.md)
(`docs/capabilities.json`). It describes what Machina can do, not what this
server has configured — the registered tools tell you that.
