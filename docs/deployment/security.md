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

- **Token compromise** gives full CMMS read/write access for the compromised client identity.
  Rotate tokens immediately if leaked. Tokens are mapped to client IDs in
  `MACHINA_MCP_TOKENS_JSON` for audit trails.
- **Network exposure:** Bind to `127.0.0.1` unless behind a reverse proxy with TLS.
  Machina does not terminate TLS itself.

**Mitigations:**

- Generate strong tokens: `openssl rand -hex 32`
- Use `MACHINA_MCP_TOKENS_JSON` (not the legacy comma-separated format) to map
  each token to a named client identity.
- Place behind a reverse proxy (nginx, Caddy, Envoy) for TLS termination.
- Restrict network access via firewall rules to known MCP client IPs.

### DocumentStore (RAG Ingestion)

**Trust level:** Ingested documents are untrusted content.

**Risk:** A malicious document could contain prompt-injection payloads that
attempt to manipulate the LLM into unauthorized actions (e.g., "ignore previous
instructions and create a work order for...").

**Mitigations:**

- Machina's prompt templates include injection-defense preambles.
- Sandbox mode (`MACHINA_SANDBOX_MODE=true`) prevents any write from executing —
  writes are logged but not sent to the CMMS.
- Review ingested documents before adding them to the vector store in production.

### Trace JSONL Files

**Trust level:** Internal diagnostic data.

**Risk:** Trace files may contain:

- Tool call arguments (asset IDs, work order descriptions)
- LLM cost data
- Conversation IDs

They do **not** contain raw LLM prompts or API keys (redacted by the `ActionTracer`).

**Mitigations:**

- Store traces in a directory with restricted permissions (`chmod 750`).
- The trace exporter supports field redaction — configure it to strip sensitive
  fields before shipping to external systems.
- Rotate and archive traces periodically.

## Secrets Management

### Baseline: Environment Variables

The simplest approach — secrets live in an environment file:

```bash
# /etc/machina/machina.env (systemd)
# chmod 600, owned by root
OPENAI_API_KEY=sk-...
MACHINA_MCP_TOKENS_JSON={"token1": "client-a", "token2": "client-b"}
MACHINA_CMMS_PASSWORD=...
```

For Docker, use `.env` with `docker compose`:

```bash
# deploy/docker/.env (not committed to version control)
OPENAI_API_KEY=sk-...
```

### Advanced: External Secret Stores

For production deployments with stricter compliance requirements:

**HashiCorp Vault / Azure Key Vault:**
Wrap the Machina start command in a script that fetches secrets and injects
them as environment variables:

```bash
#!/bin/bash
export OPENAI_API_KEY=$(vault kv get -field=key secret/machina/openai)
export MACHINA_CMMS_PASSWORD=$(vault kv get -field=password secret/machina/cmms)
exec /opt/machina-venv/bin/python -m machina.mcp \
    --transport streamable-http \
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
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM provider calls | Restart required |
| `MACHINA_MCP_TOKENS_JSON` | MCP client auth | Restart required; coordinate with MCP clients |
| `MACHINA_CMMS_PASSWORD` | CMMS connector | Restart required |
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
`python:3.11-slim` as the base. To pin the base image digest:

```dockerfile
FROM python:3.11-slim@sha256:<digest> AS builder
```

### Vulnerability Scanning

```bash
# Scan Python dependencies
pip-audit --requirement requirements.txt

# Scan container image
docker scout cves machina:latest
# or
trivy image machina:latest
```

## Hardening Checklist

- [ ] Use streamable-http transport (not stdio) in multi-user environments
- [ ] Generate unique MCP tokens per client with `openssl rand -hex 32`
- [ ] Map tokens to client identities in `MACHINA_MCP_TOKENS_JSON`
- [ ] Place behind a TLS-terminating reverse proxy
- [ ] Restrict `/etc/machina/machina.env` to `chmod 600`
- [ ] Enable sandbox mode during initial deployment (`MACHINA_SANDBOX_MODE=true`)
- [ ] Restrict trace file directory permissions (`chmod 750`)
- [ ] Review documents before ingesting into DocumentStore
- [ ] Run `pip-audit` or equivalent in CI
- [ ] Pin the Docker base image digest in production builds
