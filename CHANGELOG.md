# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`WorkflowContext.resolve_input_value`** for step-input resolution that preserves raw object types. The existing `resolve()` always returns a `str` via `re.sub`, which silently coerces complex outputs (dicts, `WorkOrder` instances) to their `str` repr. The new method returns the raw referenced value when the entire template is a single `{key}` placeholder, and falls back to `resolve()` for templates with surrounding text. This lets workflow steps pass complex objects between steps — e.g. the `WorkOrder` produced by `work_order_factory.create` now flows into `cmms.create_work_order(work_order=…)` without coercion. Backward compatible: text-with-placeholder templates still produce strings.
- **`examples` extra** in `pyproject.toml`, pulling in `python-dotenv`. The example preflight now loads `examples/.env` automatically so users can keep API keys in a local file instead of exporting shell variables every session. The import is wrapped in `try`/`except` so users on `machina-ai[litellm]` without the new extra are unaffected.
- **Shared CLI helper** `examples/_mode.py` exposing `add_mode_flags()` and `resolve_sandbox()`. Every example agent and the `odl-generator-from-text` template now accept mutually exclusive `--sandbox` and `--live` flags. `--help` advertises which mode is the default (LIVE for `quickstart`, SANDBOX everywhere else).
- **CLI consistency tests** (`tests/unit/test_examples_mode_helper.py`, `tests/unit/test_template_mode_parity.py`, `tests/e2e/test_examples_cli_consistency.py`). The e2e test auto-discovers every `agent.py` under `examples/` and `templates/`, so new examples are covered without manual registration.

### Changed

- **`predictive_pipeline` default mode flipped from LIVE to SANDBOX** for safety. Pass `--live` to execute writes.
- **`quickstart` keeps LIVE as the default** (Q&A is read-mostly), but the CLI now accepts `--live` and `--sandbox` symmetrically and the help text annotates which is the default.
- **Documentation**: `examples/quickstart/README.md` now shows the `.env` workflow alongside `export` / `$env:` / `set` syntax for bash, PowerShell, and CMD. Install command updated to `pip install "machina-ai[litellm,docs-rag,examples]"`.

### Fixed

- **Document source paths no longer leak into LLM responses.** `DocumentChunk.source` flowed verbatim into both the context-gathering payload and the `search_documents` tool result, so absolute paths like `C:\Users\foo\bar\manual.md` reached the LLM and surfaced in citations. The redaction in `ActionTracer` protected logs but not the LLM-visible payload. Sanitisation now happens at both runtime boundaries via a small `_safe_source` helper that strips directory components from path-like strings while passing through opaque IDs and URLs. `format_document_results` also calls it as defence in depth. A new Guideline 8 in the system prompt forbids disclosure of absolute paths, directory structures, database schemas, or system architecture as backstop. The raw `chunk.source` is preserved for non-LLM consumers (logs, traces).
- **`Agent.sandbox` mutations now propagate to the `WorkflowEngine`.** The engine was constructed inside `Agent.__init__` with a snapshot of the sandbox flag; mutating `agent.sandbox` afterward (the pattern every example uses when `--live` is passed) left the engine's copy stuck on the construction-time value. The CLI banner read `Mode: LIVE` but workflow logs showed `sandbox=True` and every write went through `sandbox_service` / `sandbox_connector` interception. `Agent.sandbox` is now a `@property` with a setter that writes through to `self._engine.sandbox` — single mutation point, no behaviour change for the three existing `if self.sandbox` read sites.
- **`alarm_to_workorder` built-in workflow steps now consume upstream outputs.** `generate_work_order` and `submit_work_order` had no `inputs={...}` declaration, so the engine dispatched them with empty kwargs — sandbox logs showed `inputs={}` and live runs would have failed on missing arguments. The two steps now declare explicit `inputs` mapping `asset_id`, `failure_mode` (from `analyze_alarm` output via the new raw-passthrough behaviour described above), `description` (templated with alarm and asset IDs), and `work_order` (the factory output flowing into the CMMS connector).
- **Unified `Agent.channels` with the connector registry** ([#31](https://github.com/LGDiMaggio/machina/issues/31)). Channels passed via `Agent(channels=[...])` are now registered into the `ConnectorRegistry`, so workflow steps dispatched via `channels.send_message` (e.g. `alarm_to_workorder.notify_technician`) correctly route through them. Previously only `connectors=[...]` was discoverable by capability-based dispatch, and channel-only agent configurations silently returned `{"sent": False, "error": "No communication connector available"}`. Channels passed to both `connectors=` and `channels=` as the same instance are deduplicated by identity.
- **Sandbox now gates channel lifecycle** ([#31](https://github.com/LGDiMaggio/machina/issues/31)). With `sandbox=True`, `Agent.start()` and `Agent.stop()` skip `channel.connect()` / `channel.disconnect()`, so `EmailConnector` no longer performs real SMTP logins and other channels (Slack, Telegram) no longer open outbound sockets in sandbox mode.
- **Example CLI conventions unified.** Previously three examples accepted only `--sandbox` (default LIVE), two accepted only `--live` (default SANDBOX), and `--live` on the quickstart raised an argparse error. Every example agent and the template now accept both flags consistently. Preflight error messages route to stderr.

## [0.3.0] - 2026-04-20

### Added

- **MCP Server** — expose Machina connectors via Model Context Protocol. Supports `stdio` (IDE integration) and `streamable-http` (multi-client deployment) transports. Tools are auto-registered from connector capabilities. Includes resources (asset details, work orders, failure taxonomy) and pre-built prompts (diagnosis, preventive planning, history summary).
- **MCP Authentication** — static bearer token auth with per-token client identity tracking (`MACHINA_MCP_TOKENS_JSON`). Pluggable `TokenVerifier` protocol for Vault/AKV integration.
- **Typed Capability enum** — `Capability` enum replaces `list[str]` for connector capabilities. Dual-accept registry preserves backward compatibility through v0.3.x.
- **Excel/CSV Connector** (`ExcelCsvConnector`) — read/write maintenance data from `.xlsx` and `.csv` files with YAML schema mapping and file watcher support.
- **SQL Connector** (`GenericSqlConnector`) — read from PostgreSQL, SQL Server, SQLite, DB2 with YAML table-to-entity mapping.
- **GenericCmms YAML Mapper** — zero-Python entity mapping for any REST CMMS. Declarative field specs with coercers (`enum_map`, `regex_extract`, `datetime`), reverse mapping for writes, and pluggable coercer registry.
- **ActionTracer v2** — `conversation_id` field groups traces by conversation. LLM cost tracking (`prompt_tokens`, `completion_tokens`, `usd_cost`, `model`). JSONL export with automatic secret redaction and summary truncation.
- **Docker deployment** — multi-stage Dockerfile, docker-compose with Machina + ChromaDB + mock CMMS, `.env.example` with all configuration variables documented.
- **systemd deployment** — production-ready `machina.service` unit with security hardening (`ProtectSystem=strict`, `NoNewPrivileges`, dedicated user/group).
- **Starter-kit template** (`templates/odl-generator-from-text/`) — clone-configure-deploy package: Italian free-text message → asset resolution → Work Order creation. Dual substrate (Excel / REST CMMS). Email + Telegram channels. 20 PMI-Italia sample assets. Italian entity-resolver prompt with typo/abbreviation/synonym tolerance.
- **Deployment documentation** — on-premise guide, Docker guide, uptime/resilience doc (16-combination behavior matrix), security doc (threat model for stdio/HTTP/DocumentStore/traces), scaling doc (why CPU autoscaling is wrong for LLM workloads), secrets management decision matrix.
- **MCP documentation** — setup, tools reference, resources, prompts, auth configuration.
- **Observability documentation** — action traces format, JSONL export, cost tracking and analysis.
- **Connector documentation** — Excel/CSV, SQL, GenericCmms YAML mapper guides.
- **Migration guide** — v0.2 → v0.3 checklist (5-minute upgrade path for custom connector authors).

### Changed

- Connector `capabilities` property type: `list[str]` → `frozenset[Capability]`. The old format is still accepted (dual-accept registry) but will be removed in v0.4.
- `machina.mcp.MCPServer` stub replaced by a real MCP server implementation (FastMCP-based).
- `mkdocs.yml` navigation expanded with MCP, Templates, Deployment, and Observability sections.
- Top-level README updated with Starter Kit section.

### Deprecated

- `list[str]` capability format on connectors — migrate to `frozenset[Capability]` before v0.4.
- `MACHINA_MCP_TOKENS` (comma-separated) — use `MACHINA_MCP_TOKENS_JSON` for per-token client identity.

### Removed

- `MCPServer` `NotImplementedError` stub — replaced by real implementation.

### Deferred to v0.3.1

- Kubernetes manifests, Helm charts, HPA configuration
- Conversation replay API (`ActionTracer.for_conversation()`)
- Alerting hooks (`ActionTracer.on_alert`)
- Templates: technician-chatbot, predictive-workflow
- MCP resource URI scheme promotion from pre-stable to stable
- OAuth 2.1 authorization server for MCP
- WhatsApp connector (pending Meta approval)
- MaintainX dedicated connector (GenericCmms YAML covers the use case)

## [0.2.1] - 2026-04-15

A focused consolidation release between v0.2.0 and v0.3. No new features; the goal was an honest, stable base ahead of the MCP server layer work in v0.3. No public API removed.

### Added

- **`docs/roadmap.md`** — what ships in v0.2.1 and what's planned for v0.3 (MCP server, `#31` channels/registry unification, MaintainX/Limble/Fiix, `AgentTeam`, anomaly detection, plugin system, WhatsApp/Teams).
- **`docs/troubleshooting.md`** — short entries for the issues adopters hit most: LLM provider model strings, sandbox vs live mode, connector capability discovery, config-loader errors.
- **Loud stub for `machina.mcp.MCPServer`** — instantiation raises `NotImplementedError` with a pointer to the roadmap. `import machina.mcp` continues to work, reserving the import path across the v0.2 → v0.3 jump.
- **`EmailConnector`** — available as a communication connector for workflow notification. See `docs/connectors/email.md` for setup.
- **LiteLLM contract tests** (`tests/unit/test_llm_provider.py::TestLiteLLMModelStringContract`) — exercise the real `litellm.get_llm_provider` parser, pinning the `provider:model → provider/model` normalization introduced in `b48f649` and anchoring that the colon form keeps being rejected by LiteLLM.
- **`tests/validate_examples.py` construct check** — now imports every runnable `examples/*/agent.py` so module-level `Agent(...)` construction actually runs. Catches the "imports fine but blows up at first call" class of regression that produced the post-v0.2.0 reactive-fix cadence.
- **Per-module coverage floors in CI** (agent 88%, config 95%, llm 95%, observability 85%, workflows 90%). Floors sit ~5% below the measured baseline; any silent regression in a core module now trips CI.

### Changed

- `docs/mcp-server.md` warning admonition — describes the new import-OK / instantiate-raises behaviour and links the new `docs/roadmap.md`.
- Test layout — contract tests live alongside fake-based tests in `tests/unit/test_llm_provider.py` (one test file per source file, per `CLAUDE.md` convention).

### Fixed

- No code fixes beyond the honesty cleanup above; v0.2.0 shipped stably and this release is scaffolding.

### Deprecated

- None.

### Removed

- None. `machina.mcp` import path is preserved; it was empty before and is a loud stub now, but still importable.

### Notes

- A framework gap surfaced during consolidation: workflow notification steps resolve channels via the connector registry while `Agent(channels=[...])` lives on a separate list, and `sandbox=True` does not gate `channel.connect()`. Tracked in [#31](https://github.com/LGDiMaggio/machina/issues/31) for v0.3.

## [0.2.0] - 2026-04-11

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
