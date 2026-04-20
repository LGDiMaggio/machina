# MCP Resources

Machina exposes plant data as MCP resources with a versioned URI scheme.

!!! note "Pre-stable"
    The `machina://v1/` URI scheme is **pre-stable** in v0.3.0. URIs may change
    in v0.3.1 when the scheme is locked. Do not build hard dependencies on
    specific URI patterns yet.

## Available Resources

### Asset Details

**URI:** `machina://v1/assets/{asset_id}`

Returns JSON with full asset details: ID, name, type, location, criticality,
manufacturer, model, failure modes, and metadata.

```json
{
  "id": "P-201",
  "name": "Centrifugal Pump — Cooling Loop A",
  "type": "rotating_equipment",
  "location": "Building A / Floor 1 / Bay 3",
  "criticality": "A",
  "manufacturer": "Grundfos",
  "model": "CR 32-2",
  "failure_modes": ["BEAR-WEAR-01", "SEAL-LEAK-01", "IMP-EROS-01"]
}
```

### Work Order Details

**URI:** `machina://v1/work-orders/{wo_id}`

Returns JSON with work order details: ID, type, priority, status, asset ID,
description, and assignment.

### Failure Taxonomy

**URI:** `machina://v1/failure-taxonomy`

Returns the built-in failure mode taxonomy — a reference list of common
industrial failure modes with codes, categories, mechanisms, and detection
methods. This resource is served from memory (no connector required).

```json
[
  {
    "code": "BEAR-WEAR-01",
    "name": "Bearing Wear",
    "category": "mechanical",
    "mechanism": "Fatigue, lubrication breakdown, contamination",
    "detection_methods": ["vibration_analysis", "temperature_monitoring", "oil_analysis"]
  }
]
```

## MIME Types

All resources return `application/json`.

## Resource Discovery

MCP clients can list available resources dynamically. The resource list depends
on which connectors are configured — if no CMMS is configured, asset and work
order resources are not available. The failure taxonomy is always available.
