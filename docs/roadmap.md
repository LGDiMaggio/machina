# Roadmap

## v0.4 — Current release

Released 2026-10-04. Highlights (full list in the [changelog](changelog.md)):

- **MCP server** you can run: `machina mcp serve` over stdio or streamable HTTP
  with bearer-token auth, 15 capability-gated tools, 4 resources and 3 prompts,
  plus a working Docker/systemd deployment.
- **Runtime-enforced write safety**: human-in-the-loop confirmation on by
  default in the agent, a resolution-authority gate on work-order creation,
  sandbox mode at the connector boundary, and work-order creation that is
  idempotent on the Generic CMMS, Excel/CSV and SQL connectors.
- **Substrates from YAML**: the Excel/CSV, SQL and Generic CMMS connectors (and
  the vendor CMMS auth blocks) build from a `machina.yaml`, failure-mode
  catalogs are a declared capability, and the `odl-generator-from-text` starter
  kit runs end to end.
- **Documentation checked against the code**, including the generated
  [capability matrix](capabilities.md).

## Earlier releases

- **v0.3.1** — write-path safety and idempotency (deterministic work-order IDs,
  layer-wide sandbox enforcement, atomic local persistence, method-aware
  retries) and the RAG upgrade (hybrid retrieval, reranking, parent-document
  chunking, layout-aware parsing, citations).
- **v0.3.0** — the MCP server layer, typed connector capabilities, the Excel/CSV
  and SQL connectors, the Generic CMMS YAML mapper, the deployment story and
  the first starter kit.
- **v0.2.x** — workflow engine, OPC-UA and MQTT connectors, Slack, Email and
  Calendar, sandbox mode, security hardening.
- **v0.1** — domain model, SAP PM / Maximo / UpKeep connectors, document store
  with RAG, Telegram, agent runtime.

## v0.5 — Next

Ordered by what moves adoption the most:

1. **More CMMS connectors** — MaintainX, Limble, Fiix, on the same
   connector/capability pattern as SAP PM, Maximo and UpKeep.
2. **WhatsApp and Teams** communication connectors.
3. **Anomaly detection and RUL estimation** on top of the IoT connector
   streams.
4. **Multi-agent orchestration.**

Also planned: removing the deprecated raw-string capability forms (declare
`frozenset[Capability]`).

## Later

- A plugin system for community-contributed connectors without forking the core
  package.
- Non-Python SDKs (Go / TypeScript clients).

Machina stays a framework, not a hosted product.

## MCP direction (standing position)

MCP is **transport**, not a replacement for connectors. The connector layer is
Machina's normalization layer — it maps vendor payloads onto the canonical
maintenance domain — and that, with the write-path invariants and typed
capabilities on top, is the moat. MCP carries normalized data; it does not
produce it.

- **The internal flip is rejected.** We do not rewire Machina's own
  runtime↔connectors boundary to speak MCP, and we do not replace connectors
  with a bag of MCP tools. Internal boundaries stay native Python; MCP lives only
  at the edge.
- **The transport/mapper split already future-proofs against vendor MCPs.** When
  a CMMS vendor ships its own MCP server, that becomes a new *transport* feeding
  the existing per-vendor mappers (`connectors/cmms/mappers/`) — a new fetch path,
  not a re-normalization. The durable work (mapping) is insulated from transport.
- **Outbound MCP (Machina as an MCP server) exists** — every connector's
  capabilities can be exposed as MCP tools (see [MCP Server](mcp-server.md)).

### Gated: inbound MCP-client connector

A generic **inbound** connector — Machina as an MCP *client*, consuming a vendor's
MCP server *into* the domain model (the inverse of the outbound server). It would
be built as transport (a generic MCP client) plus per-vendor mappers, reusing the
same transport/mapper split. **Gated behind the trigger "first real vendor CMMS
MCP" — not built now.** Until a CMMS vendor actually ships an MCP server worth
consuming, a generic MCP-client adapter would be speculative surface with nothing
to validate it against.

## How to steer the roadmap

- New connector or integration idea → open an issue labelled `connector` describing the system, its API, and a minimal capability set.
- Framework bug or papercut → issue with a failing test case when possible.
- Strategic disagreement ("this should be a higher priority") → open a discussion; the ordering above is the maintainer's current best guess, not a commitment.
