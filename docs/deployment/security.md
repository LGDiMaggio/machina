# Security

Threat model, secrets management, and supply-chain considerations for Machina deployments.

## Threat Model

### Trust Boundaries

```
┌─────────────────────────────────────────────────────┐
│  Machina Process                                     │
│  ┌──────────┐  ┌───────────┐  ┌──────────────────┐ │
│  │ MCP      │  │ Connector │  │ DocumentStore    │ │
│  │ Transport│──│ Layer     │──│ (RAG)            │ │
│  └────┬─────┘  └─────┬─────┘  └────────┬─────────┘ │
└───────┼──────────────┼────────────────┼─────────────┘
        │              │                │
   MCP Client      CMMS / IoT     Ingested Documents
   (trusted?)      (trusted)      (untrusted content)
```

### Transport: stdio

**Trust level:** The MCP client is a local process on the same machine.

**Risk:** Any local process can connect. There is no authentication —
stdio mode grants full CMMS write access to whoever invokes the process.

!!! danger "Single-user environments only"
    stdio mode is designed for local development and single-user IDE integrations
    (Claude Desktop, Cursor). Do not use it in multi-user or server environments.
    Use streamable-http with bearer token auth instead.

### Transport: streamable-http

**Trust level:** The MCP client authenticates with a static bearer token.

**Risk profile:**

- **Token compromise** gives the holder every registered tool, writes included —
  there are no per-token permissions. Rotate tokens immediately if leaked.
  `MACHINA_MCP_TOKENS_JSON` maps each token to a client ID, which is attached to
  the verified request; v0.4 does not yet write it to logs, traces or CMMS
  records, so it is not an audit trail on its own.
- **Network exposure:** Bind to `127.0.0.1` unless behind a reverse proxy with TLS.
  Machina does not terminate TLS itself.

**Mitigations:**

- Generate strong tokens: `openssl rand -hex 32`. The server refuses to start
  with any token shorter than 32 characters.
- Use `MACHINA_MCP_TOKENS_JSON` (not the legacy comma-separated format), one
  token per client, so a single client can be revoked.
- Keep `mcp.allowed_hosts` / `mcp.allowed_origins` to the names clients really
  use (loopback by default): requests with another `Host` or `Origin` are
  rejected, which blocks DNS-rebinding attacks.
- Place behind a reverse proxy (nginx, Caddy, Envoy) for TLS termination.
- Restrict network access via firewall rules to known MCP client IPs.

### DocumentStore (RAG Ingestion)

**Trust level:** Ingested documents are untrusted content.

**Risk:** A malicious document could contain prompt-injection payloads that
attempt to manipulate the LLM into unauthorized actions (e.g., "ignore previous
instructions and create a work order for...").

**Mitigations:**

- Machina's prompt templates include injection-defense preambles.
- Sandbox mode (`sandbox: true` in the config; the shipped deploy configs read
  it from `MACHINA_SANDBOX_MODE`) prevents any write from executing — writes
  are logged but not sent to the CMMS.
- Outside sandbox, the agent asks for confirmation before every write by
  default (`confirmations: true`). This is an `Agent` feature: the MCP server
  has no confirmation step — the MCP client and its user decide which tool
  calls run — so keep an MCP server in sandbox mode until you trust that
  client.
- Review ingested documents before adding them to the vector store in production.

#### Source-Path Sanitisation at the LLM Boundary

**Risk:** The raw `DocumentChunk.source` returned by a connector is typically an
absolute filesystem path (e.g. `C:\Users\foo\bar\manual.md` or
`/var/data/manuals/pump.md`). Passing it verbatim into the LLM-visible context
or the `search_documents` tool result exposes the host's filesystem layout —
any model asked to cite a source will repeat the path, sometimes prefixed with
the user account name. This is a deterministic leak rooted in the framework's
own payload, not an LLM hallucination.

**Mitigations (always-on):**

- The two runtime boundaries that build LLM-visible document payloads
  (`Agent._gather_context` and the `search_documents` tool result) pass every
  source through `agent.prompts.safe_source`, which strips directory
  components from path-like strings and passes through opaque IDs and URLs.
- `format_document_results` invokes `safe_source` again as defence in depth,
  so any future call site that forgets the sanitisation upstream is still
  protected at the prompt-formatting layer.
- **Beyond the `source` field (v0.3.1):** absolute paths can also be embedded
  in the chunk **content** text itself and in **workflow error / output
  strings** that flow back to the LLM. `agent.prompts.safe_text` scrubs those
  — it reduces identity-/infrastructure-revealing paths (user-home dirs like
  `C:\Users\<user>\…`, `/home/<user>/…`, and UNC shares `\\host\share\…`) to
  their basename, while deliberately preserving instructional system paths
  (`/etc`, `/usr/bin`, `C:\Program Files`) so technical-manual fidelity is
  kept. It is applied to chunk content in both runtime boundaries and to the
  `execute_workflow` step `output_summary` / `error` payloads.
- The system prompt's Guideline 8 explicitly forbids the LLM from disclosing
  absolute file paths, directory structures, database schemas, or system
  architecture. This is a backstop, not the primary defence — the primary
  defence is to never hand those values to the LLM in the first place.
- The raw `chunk.source` remains available for non-LLM consumers (logs,
  trace files protected separately by `ActionTracer.redacting_dump_json`).
  See [Traces / Redaction](../observability/traces.md#redaction) for the
  parallel mechanism on the trace export side.

### Sandbox Enforcement (Layer-Wide)

**Trust level:** Sandbox mode is the safety boundary for evaluation, demos, and
initial deployment — no real external side effect must execute while it is on.

**Guarantee (v0.3.1):** sandbox enforcement is a *layer-wide invariant*, not a
per-connector choice. Every external-mutation path is guarded by the
`@sandbox_aware` decorator, which raises `SandboxViolationError` before the
method body runs:

- CMMS `create_work_order` / `update_work_order` (Generic, SAP PM, Maximo, UpKeep, Excel/CSV, SQL)
- Comms `send_message` (Telegram, Slack, Email)
- MQTT `publish`
- Calendar `create_event` / `delete_event`

`CliChannel.send_message` is intentionally **exempt** — it only prints to
stdout and must keep working in sandbox so the agent can still reply.

**MCP request tasks:** each MCP tool call runs in its own asyncio task that does
**not** inherit the `_sandbox_mode` contextvar set once at server startup. Both
the domain (`mcp.tools._runtime`) and vendor (`mcp.tools_vendor._runtime`)
helpers re-establish it on every request, and the vendor tools read
`get_sandbox_mode()` only *after* that — otherwise a raw vendor write (e.g. the
Maximo OData PATCH, which has no decorator backstop) would execute live in
sandbox mode.

**Companion invariant — write integrity:** the write paths also guard
against duplicates. Auto-generated work-order IDs are a deterministic content
hash (`auto_work_order_id`); the Excel/CSV, SQL and local Generic CMMS
connectors return the existing work order when its ID is already there, and
the Generic CMMS REST mode sends the ID to the backend. The agent loop
memoises side-effecting tools per turn. The vendor connectors' HTTP retries
are method-aware: POST and PATCH are *not* retried on network errors,
timeouts or 503 answers, since any of those can follow a write the server
already made; 429 answers are retried for every method because the server
refused the request without processing it. SAP PM, Maximo and UpKeep let the
CMMS number new work orders, so these guards do not deduplicate a create
retried later against them (see [Uptime](uptime.md#transient-failure-handling)).

### Trace JSONL Files

**Trust level:** Internal diagnostic data.

**Risk:** Trace files (written only when you attach a `JSONLExporter`) may
contain:

- The first 200 characters of each tool result and workflow step output
  (asset names, work-order descriptions, …)
- Asset IDs, connector names and LLM token/cost data

They do **not** contain prompts or message text, and metadata values under
secret-like keys (`token`, `password`, `secret`, `api_key`, `client_secret`,
`authorization`) are redacted on export.

**Mitigations:**

- The exporter creates its directory with mode `0700` and files with `0600`
  (POSIX); keep them there.
- Review what tool results reveal before shipping trace files to external
  systems.
- Rotate and archive traces periodically (the exporter starts a new file each
  UTC day).

## Secrets Management

### Baseline: Environment Variables

The simplest approach — secrets live in an environment file, and the YAML
config references them as `${VAR}` placeholders (a missing variable fails the
start instead of falling back):

```bash
# /etc/machina/machina.env (systemd)
# chmod 600, owned by root
MACHINA_MCP_TOKENS_JSON={"<64-hex token>": "client-a", "<64-hex token>": "client-b"}
MACHINA_CMMS_API_KEY=...
```

For Docker, use `.env` with `docker compose` (`deploy/docker/.env`, never
committed). The MCP server needs no LLM key; an agent you run needs its
provider's key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …), read by LiteLLM.

### Advanced: External Secret Stores

For production deployments with stricter compliance requirements:

**HashiCorp Vault / Azure Key Vault:**
Wrap the Machina start command in a script that fetches secrets and injects
them as environment variables:

```bash
#!/bin/bash
export MACHINA_MCP_TOKENS_JSON=$(vault kv get -field=tokens secret/machina/mcp)
export MACHINA_CMMS_API_KEY=$(vault kv get -field=api_key secret/machina/cmms)
exec /opt/machina-venv/bin/machina mcp serve \
    --transport streamable-http \
    --host 127.0.0.1 \
    --config /etc/machina/config.yaml
```

Update the systemd unit's `ExecStart` to point to this wrapper script.

**SOPS for GitOps:**
Encrypt your `.env` or `config.yaml` with [SOPS](https://github.com/getsops/sops)
and decrypt at deploy time:

```bash
sops --decrypt machina.env.enc > /etc/machina/machina.env
chmod 600 /etc/machina/machina.env
```

### What to Protect

| Secret | Where used | Rotation impact |
|--------|-----------|-----------------|
| `MACHINA_MCP_TOKENS_JSON` | MCP client auth | Restart required; coordinate with MCP clients |
| CMMS credentials (e.g. `MACHINA_CMMS_API_KEY`) | CMMS connector, via `${VAR}` in the config | Restart required |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM calls of agents you run (not the MCP server) | Restart required |
| OPC-UA certificates | IoT connector | Restart required; re-establish subscriptions |

## Supply Chain

### Dependencies

Machina's Python dependencies are declared in `pyproject.toml` with version constraints.
Review the dependency tree before deploying:

```bash
pip install pipdeptree
pipdeptree --packages machina-ai
```

### Container Image

The Docker image (`deploy/docker/Dockerfile`) uses a multi-stage build with
`python:3.11-slim-bookworm` as the base. To pin the base image digest:

```dockerfile
FROM python:3.11-slim-bookworm@sha256:<digest> AS build
```

### Vulnerability Scanning

```bash
# Scan the Python dependencies installed in the Machina environment
/opt/machina-venv/bin/python -m pip install pip-audit
/opt/machina-venv/bin/python -m pip_audit

# Scan container image
docker scout cves machina:latest
# or
trivy image machina:latest
```

## Hardening Checklist

- [ ] Use streamable-http transport (not stdio) in multi-user environments
- [ ] Generate unique MCP tokens per client with `openssl rand -hex 32`
- [ ] Map tokens to client identities in `MACHINA_MCP_TOKENS_JSON`
- [ ] Place behind a TLS-terminating reverse proxy, and list its host name in `mcp.allowed_hosts`
- [ ] Restrict `/etc/machina/machina.env` to `chmod 600`
- [ ] Enable sandbox mode during initial deployment (`sandbox: true`, or `MACHINA_SANDBOX_MODE=true` with the shipped configs)
- [ ] Keep exported trace files in the exporter's `0700` directory
- [ ] Review documents before ingesting into DocumentStore
- [ ] Run `pip-audit` or equivalent in CI
- [ ] Pin the Docker base image digest in production builds
