# Roadmap

## v0.2.1 (shipping now) — Consolidation

A focused hardening release between v0.2.0 and v0.3. No new features; the goal was an honest, stable base with its loose ends tightened:

- **Loud stub for `machina.mcp.MCPServer`.** `import machina.mcp` stays importable across the v0.2 → v0.3 transition; instantiating `MCPServer()` now raises `NotImplementedError` with a pointer back here instead of silently handing back an empty namespace.
- **`EmailConnector` wired into `examples/01_alarm_response/`** for discoverability. Set `MACHINA_SMTP_HOST` / `MACHINA_SMTP_USER` / `MACHINA_SMTP_PASSWORD` to attach it alongside `CliChannel`. Without those env vars the example stays a zero-config CLI demo.
- **LiteLLM contract test** against the real `litellm.get_llm_provider` parser. Pins the `provider:model → provider/model` normalization that produced reactive fix `b48f649`, and anchors the inverse (colon form must keep being rejected) so a future LiteLLM relaxation is noisy, not silent.
- **Extended example validator.** `tests/validate_examples.py` now imports every runnable `examples/*/agent.py` and verifies module-level `Agent(...)` construction actually runs. Catches the "imports fine but blows up at first call" class of bug.
- **Per-module coverage floors in CI** for the core modules (`agent`, `config`, `llm`, `observability`, `workflows`). Each floor sits below the measured baseline with a ~5% buffer, so normal refactor churn has headroom but a silent regression trips CI.

## v0.3 — Next

Ordered by what moves adoption the most:

1. **MCP server layer.** Expose every connector's declared capabilities as Model Context Protocol tools so Claude Desktop, Cursor, Continue, and any MCP-compatible client can talk to Machina connectors with no agent code. This is the biggest adoption multiplier on the roadmap and the reason `machina.mcp` has been reserved as a stable import path.
2. **Unify `agent.channels` with the connector registry** ([#31](https://github.com/LGDiMaggio/machina/issues/31)). Today workflow steps that use `channels.send_message` (e.g. `alarm_to_workorder.notify_technician`) resolve the target via the connector registry, while channels passed as `Agent(channels=[...])` live in a separate list and are invisible to that lookup. Same issue tracks making `sandbox=True` gate `channel.connect()` so SMTP/Slack/… do not perform live logins in sandbox mode.
3. **More CMMS connectors** — MaintainX, Limble, Fiix. Same `BaseConnector` / capability-declaration pattern as SAP PM, Maximo, UpKeep.
4. **Multi-agent orchestration** (`AgentTeam`). The `examples/05_multi_agent_team/` README describes the intended shape; the implementation lands in v0.3.
5. **Anomaly detection & RUL estimation** on top of the IoT connector stream.
6. **Plugin system** for community-contributed connectors without forking the core package.
7. **`WhatsApp` and `Teams` communication connectors.**

## What's deferred beyond v0.3

- Non-Python SDKs (Go / TypeScript clients).
- Hosted control plane. Machina stays a framework, not a product.

## How to steer the roadmap

- New connector or integration idea → open an issue labelled `connector` describing the system, its API, and a minimal capability set.
- Framework bug or papercut → issue with a failing test case when possible.
- Strategic disagreement ("this should be a higher priority") → open a discussion; the ordering above is the maintainer's current best guess, not a commitment.
