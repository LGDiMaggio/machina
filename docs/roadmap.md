# Roadmap

## v0.3.1 (next patch) — Fixes

- **Channel / connector-registry unification** ([#31](https://github.com/LGDiMaggio/machina/issues/31)). Channels passed as `Agent(channels=[...])` are now registered into the `ConnectorRegistry`, so workflow steps using `channels.send_message` (e.g. `alarm_to_workorder.notify_technician`) dispatch through them in live mode. `sandbox=True` now also gates `channel.connect()` / `channel.disconnect()` — no SMTP/Slack/Telegram logins in sandbox.

## v0.2.1 — Consolidation

A focused hardening release between v0.2.0 and v0.3. No new features; the goal was an honest, stable base with its loose ends tightened:

- **Loud stub for `machina.mcp.MCPServer`.** `import machina.mcp` stays importable across the v0.2 → v0.3 transition; instantiating `MCPServer()` now raises `NotImplementedError` with a pointer back here instead of silently handing back an empty namespace.
- **`EmailConnector`** available as a communication connector. See `docs/connectors/email.md` for setup.
- **LiteLLM contract test** against the real `litellm.get_llm_provider` parser. Pins the `provider:model → provider/model` normalization that produced reactive fix `b48f649`, and anchors the inverse (colon form must keep being rejected) so a future LiteLLM relaxation is noisy, not silent.
- **Extended example validator.** `tests/validate_examples.py` now imports every runnable `examples/*/agent.py` and verifies module-level `Agent(...)` construction actually runs. Catches the "imports fine but blows up at first call" class of bug.
- **Per-module coverage floors in CI** for the core modules (`agent`, `config`, `llm`, `observability`, `workflows`). Each floor sits below the measured baseline with a ~5% buffer, so normal refactor churn has headroom but a silent regression trips CI.

## v0.3 — Next

Ordered by what moves adoption the most:

1. **MCP server layer.** Expose every connector's declared capabilities as Model Context Protocol tools so Claude Desktop, Cursor, Continue, and any MCP-compatible client can talk to Machina connectors with no agent code. This is the biggest adoption multiplier on the roadmap and the reason `machina.mcp` has been reserved as a stable import path.
2. **More CMMS connectors** — MaintainX, Limble, Fiix. Same `BaseConnector` / capability-declaration pattern as SAP PM, Maximo, UpKeep.
3. **Multi-agent orchestration** (`AgentTeam`). Deferred to v0.3.1.
4. **Anomaly detection & RUL estimation** on top of the IoT connector stream.
5. **Plugin system** for community-contributed connectors without forking the core package.
6. **`WhatsApp` and `Teams` communication connectors.**

## MCP direction (standing position)

MCP is **transport**, not a replacement for connectors. The connector layer is
Machina's normalization layer — it maps vendor payloads onto the canonical
maintenance domain — and that, with the write-path invariants and typed
capabilities on top, is the moat. MCP carries normalized data; it does not
produce it.

- **The internal flip is rejected.** We do not rewire Machina's own
  runtime↔connectors boundary to speak MCP, and we do not replace connectors
  with a bag of MCP tools. Internal boundaries stay native Python; MCP lives only
  at the edge. (See `MACHINA_SPEC.md` §17 for the full argument.)
- **The transport/mapper split already future-proofs against vendor MCPs.** When
  a CMMS vendor ships its own MCP server, that becomes a new *transport* feeding
  the existing per-vendor mappers (`connectors/cmms/mappers/`) — a new fetch path,
  not a re-normalization. The durable work (mapping) is insulated from transport.
- **Outbound MCP (Machina as an MCP server) already exists** — every connector's
  capabilities can be exposed as MCP tools (item 1 under v0.3 above).

### Gated: inbound MCP-client connector

A generic **inbound** connector — Machina as an MCP *client*, consuming a vendor's
MCP server *into* the domain model (the inverse of the outbound server). It would
be built as transport (a generic MCP client) plus per-vendor mappers, reusing the
same transport/mapper split. **Gated behind the trigger "first real vendor CMMS
MCP" — not built now.** Until a CMMS vendor actually ships an MCP server worth
consuming, a generic MCP-client adapter would be speculative surface with nothing
to validate it against.

## What's deferred beyond v0.3

- Non-Python SDKs (Go / TypeScript clients).
- Hosted control plane. Machina stays a framework, not a product.

## How to steer the roadmap

- New connector or integration idea → open an issue labelled `connector` describing the system, its API, and a minimal capability set.
- Framework bug or papercut → issue with a failing test case when possible.
- Strategic disagreement ("this should be a higher priority") → open a discussion; the ordering above is the maintainer's current best guess, not a commitment.
