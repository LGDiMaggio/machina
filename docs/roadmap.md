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

## What's deferred beyond v0.3

- Non-Python SDKs (Go / TypeScript clients).
- Hosted control plane. Machina stays a framework, not a product.

## How to steer the roadmap

- New connector or integration idea → open an issue labelled `connector` describing the system, its API, and a minimal capability set.
- Framework bug or papercut → issue with a failing test case when possible.
- Strategic disagreement ("this should be a higher priority") → open a discussion; the ordering above is the maintainer's current best guess, not a commitment.
