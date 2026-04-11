# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Workflow Engine** with trigger-step-action model, sequential execution,
  template variable resolution (`{trigger.field}`, `{step_name.field}`),
  per-step error policies (RETRY, SKIP, STOP, NOTIFY), guard conditions,
  configurable timeouts, and action tracing
- **Sandbox mode** for safe experimentation: write actions (create, update,
  delete, send) are logged but not executed; read-only actions still run
  normally.  Sandbox enforced in both WorkflowEngine and Agent tool dispatch
- **`alarm_to_workorder` built-in workflow** — 7-step template from sensor
  alarm through diagnosis, spare part check, work order creation, technician
  notification, confirmation, and CMMS submission
- **`SlackConnector`** — Slack integration via the Bolt SDK in Socket Mode
  (WebSocket-based, no public endpoint required).  Supports channel
  whitelisting, bot-message filtering, and bidirectional messaging
- **`EmailConnector`** — Email integration with two backends:
  - Standard SMTP/IMAP (zero external dependencies, TLS/SSL support)
  - Gmail API backend via OAuth2 (`pip install machina-ai[gmail]`)
  - Polling-based inbox monitoring with persistent IMAP connections
- **`CalendarConnector`** with three pluggable backends:
  - Google Calendar API v3 (OAuth2 + service account auth)
  - Microsoft 365 / Outlook (MSAL client-credentials + Graph API)
  - iCal `.ics` files and URLs (read-only, with RRULE expansion)
  - Facade pattern with dynamic capabilities (read-only for iCal,
    full CRUD for Google/Outlook)
  - Convenience methods: `get_production_schedule()`,
    `get_planned_downtime()`, `get_technician_availability()`
- **`CalendarEvent`**, **`PlannedDowntime`**, **`ShiftPattern`** domain
  entities with `EventType` enum
- **`OpcUaConnector`** — OPC-UA client for real-time sensor data with
  subscription-based monitoring, value-to-alarm conversion, and security
  policy support (None, Sign, SignAndEncrypt)
- **`MqttConnector`** — MQTT pub-sub with JSON, Sparkplug B, and raw
  payload support.  Topic wildcards, TLS, and fan-out architecture for
  concurrent subscriptions
- **`Step.is_write`** field for explicit write-action marking, overriding
  the keyword-based heuristic in sandbox mode
- **Workflow `depends_on` validation** — the engine validates all step
  dependency references at execution start, raising `WorkflowError` for
  invalid references
- **`IncomingMessage`** and **`MessageHandler`** extracted to
  `machina.connectors.comms.types` for clean cross-connector imports
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

### Security

- **Secret redaction** in structured logs — fields matching `token`,
  `password`, `secret`, `api_key`, `client_secret`, `authorization` are
  automatically replaced with `***REDACTED***`
- **Input length limit** — `Agent.handle_message()` truncates messages
  exceeding 10,000 characters with a warning log
- **Prompt hardening** — system prompt now includes guideline rejecting
  instruction override attempts and role changes
- **Sandbox enforcement** — `Agent._tool_create_work_order()` now
  respects sandbox mode (previously bypassed)
- **Insecure connection warnings** — OPC-UA and MQTT connectors log
  warnings when security/TLS is disabled
- **Dependabot** configured for weekly pip and GitHub Actions vulnerability
  scanning
- **`.gitignore`** expanded to block `*.pem`, `*.key`, `credentials.json`,
  and client secret files
- **Auth docstring examples** updated to use `os.environ[]` instead of
  hardcoded secrets

### Fixed

- **OPC-UA task reference leak** — `_DataChangeHandler` now tracks
  background tasks in a `set` with `add_done_callback` to prevent
  garbage collection of in-flight tasks under rapid data changes
- **MQTT shared iterator bug** — replaced per-subscription `_message_loop`
  with a single `_reader_loop` fan-out architecture, preventing competing
  consumers on the `aiomqtt.Client.messages` async generator
- **Guard condition exceptions** now logged with `exc_info=True` instead
  of being silently swallowed
- **Outlook Calendar token refresh** — MSAL app instance is now stored
  and tokens are refreshed via `acquire_token_silent()` before each API
  call, preventing failures after the initial 1-hour token expiry
- **IMAP connection reuse** — `EmailConnector` now maintains a persistent
  IMAP connection across poll cycles with automatic reconnection on
  failure, reducing TCP/TLS handshake overhead
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
