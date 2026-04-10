# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 2 CMMS connectors: `SapPmConnector` (SAP PM OData), `MaximoConnector`
  (IBM Maximo OSLC/JSON), `UpKeepConnector` (UpKeep REST v2)
- `OAuth2ClientCredentials` auth strategy for SAP S/4HANA and other
  enterprise systems requiring OAuth2 machine-to-machine auth
- `SparePart.metadata` field to preserve connector-specific fields verbatim,
  consistent with `Asset.metadata` and `WorkOrder.metadata`
- Shared HTTP retry helper (`machina.connectors.cmms.retry.request_with_retry`)
  with exponential backoff on 429 / 503 responses (honouring numeric
  `Retry-After` headers) and transient network errors
  (`httpx.TimeoutException`, `httpx.ConnectError`, `httpx.ReadError`).
  All HTTP calls in `SapPmConnector`, `MaximoConnector`, and `UpKeepConnector`
  now route through it.
- `SapPmConnector.__init__` accepts `bom_service`, `bom_entity_set`,
  `bom_material_field`, `bom_equipment_field` to configure the BOM OData
  endpoint per SAP version. Defaults target
  `API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem` (standard S/4HANA Cloud).
- `MaximoConnector.__init__` accepts `asset_type_map: dict[str, AssetType]`
  that maps Maximo `classstructureid` (or `assettype`) values to Machina
  `AssetType`. Without the map the connector falls back to the historical
  default of `ROTATING_EQUIPMENT`.
- `get_work_order(id)` single-record fetch for all three CMMS connectors
  (follows the existing `get_asset()` pattern).
- `update_work_order(id, *, status, assigned_to, description)` via PATCH
  for all three CMMS connectors, with keyword-only args for partial updates.
- `close_work_order(id)` and `cancel_work_order(id)` convenience wrappers
  on all three connectors — delegate to `update_work_order` with the
  appropriate `WorkOrderStatus` transition.
- `read_work_orders(status=WorkOrderStatus)` now accepts a Machina
  `WorkOrderStatus` enum (automatically reverse-mapped to native CMMS code)
  in addition to raw status strings, for all three connectors.
- Failure-mode mapping: `SapPmConnector` now extracts
  `MaintenanceActivityType` → `WorkOrder.failure_mode` and
  `MaintenanceCause` → `WorkOrder.failure_cause`; `MaximoConnector` extracts
  `failurecode` → `failure_mode` and `failureremark` → `failure_cause`.

### Fixed

- `SapPmConnector` CSRF token flow: `_fetch_csrf_token` replaced by
  `_write_with_csrf` which performs the CSRF fetch and the write (POST/PATCH)
  within a **single** `httpx.AsyncClient` context, sharing session cookies.
  The previous implementation used separate HTTP sessions, which caused
  SAP to reject the CSRF token with 403 on most configurations.
- `MaximoConnector._parse_spare_part` and `UpKeepConnector._parse_spare_part`
  now preserve unknown fields in `SparePart.metadata` (previously dropped)
- `UpKeepConnector._parse_spare_part` now prefers `partNumber` / `barcode`
  as the SKU, falling back to the UpKeep record `id` only when neither is
  available — the previous implementation conflated internal record IDs
  with physical part identifiers
- `UpKeepConnector` priority mapping corrected to match the UpKeep REST API
  v2 0-indexed scale (0 = lowest, 3 = highest). Previously used an
  off-by-one 1-4 scale which mislabelled every work order's priority.
- `SapPmConnector.read_spare_parts` previously pointed at the non-existent
  `API_EQUIPMENT/EquipmentBOM` entity set. The default now targets
  `API_BILL_OF_MATERIAL_SRV/BillOfMaterialItem`; users on legacy SAP
  versions can override via constructor parameters.

### Changed

- **BREAKING:** `MaximoConnector.read_spare_parts` and
  `UpKeepConnector.read_spare_parts` no longer accept the `asset_id`
  parameter. The previous implementation filtered on
  `SparePart.compatible_assets`, which was never populated by either
  parser — the feature was silently returning empty lists.
  `SapPmConnector.read_spare_parts` retains `asset_id` but the filter is
  now routed through the configurable `bom_equipment_field` and is
  silently dropped (with a warning log) when no equivalent field exists
  on the configured BOM entity set.
- **BREAKING:** `UpKeepConnector` `_reverse_priority` / `_UPKEEP_PRIORITY_MAP`
  switched from a 1-4 scale to the correct 0-3 scale. Callers that were
  constructing raw UpKeep payloads assuming the old scale must update
  their code; callers going through `Priority` enums are unaffected.

## [0.1.1] - 2026-04-07

### Added

- `.zenodo.json` metadata for automatic Zenodo DOI generation
- `CITATION.cff` for academic citation

## [0.1.0] - 2026-04-07

### Added

- Project scaffolding: pyproject.toml, CI, linting, testing setup
- Core domain model: `Asset`, `WorkOrder`, `FailureMode`, `SparePart`, `Alarm`, `MaintenancePlan`, `Plant`
- `BaseConnector` protocol and `ConnectorRegistry`
- Exception hierarchy (`MachinaError` and subclasses)
- Configuration system with YAML and environment variable support
- LLM abstraction layer (LiteLLM wrapper) with function-calling tool definitions
- Structured logging with structlog and action tracing (`ActionTrace`)
- `GenericCmmsConnector` for JSON/CSV-based CMMS integration
- `TelegramConnector` for maintenance notifications and commands
- `DocumentStore` connector with RAG (ChromaDB + LangChain document loaders)
- `Agent` runtime with domain-aware prompting, tool dispatch, and conversation loop
- `EntityResolver` for natural language → Asset/WorkOrder resolution
- `FailureAnalyzer`, `WorkOrderFactory`, `MaintenanceScheduler` domain services
- `knowledge_agent` quickstart example
- 306 unit tests, 98% coverage
