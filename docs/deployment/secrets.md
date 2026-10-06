# Secrets Management

How to handle API keys, CMMS credentials, and MCP tokens across deployment methods.

## Decision Matrix

| Environment | Method | Complexity |
|-------------|--------|------------|
| Local dev / evaluation | `.env` file | Low |
| Single-host production (systemd) | `/etc/machina/machina.env` (chmod 600) | Low |
| Docker / Docker Compose | `.env` file (not committed) | Low |
| Enterprise (compliance requirements) | Vault / Azure Key Vault + wrapper script | Medium |
| GitOps pipeline | SOPS-encrypted `.env` + decrypt at deploy | Medium |

## Secrets Inventory

| Secret | Used by | Rotation |
|--------|---------|----------|
| `MACHINA_MCP_TOKENS_JSON` | MCP server auth (each token ≥ 32 characters) | Restart; coordinate with clients |
| CMMS credentials (e.g. `MACHINA_CMMS_API_KEY`, or a username/password pair) | CMMS connector | Restart required |
| OPC-UA certificates | IoT connector | Restart; re-create subscriptions |
| Telegram bot token | Telegram connector | Restart required |
| SMTP/IMAP credentials | Email connector | Restart required |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM calls of agents you run (read by LiteLLM; the MCP server needs none) | Restart required |

Machina reads the MCP token variables and the LLM keys (through LiteLLM)
directly. Every other secret reaches a connector through a `${VAR}`
placeholder in your YAML config, so the variable names are yours to choose —
the deploy configs use `MACHINA_CMMS_URL` and `MACHINA_CMMS_API_KEY`. A
placeholder without a default fails the start when the variable is missing.

## Baseline: Environment Files

### systemd

```bash
sudo cp deploy/systemd/machina.env.example /etc/machina/machina.env
sudo chmod 600 /etc/machina/machina.env
sudo chown root:root /etc/machina/machina.env
```

systemd reads the `EnvironmentFile` before dropping privileges to the `machina` user.
The file is not readable by the running process — only by root at service start.

### Docker

```bash
cp deploy/docker/.env.example deploy/docker/.env
# Edit .env with your secrets
# NEVER commit .env to version control
```

Docker Compose loads `.env` automatically; `deploy/docker/.gitignore` already
excludes it.

## Advanced: External Secret Stores

### HashiCorp Vault

Create a wrapper script that fetches secrets and execs the server:

```bash
#!/bin/bash
export MACHINA_MCP_TOKENS_JSON=$(vault kv get -field=tokens secret/machina/mcp)
export MACHINA_CMMS_API_KEY=$(vault kv get -field=api_key secret/machina/cmms)
exec /opt/machina-venv/bin/machina mcp serve \
    --transport streamable-http \
    --host 127.0.0.1 \
    --config /etc/machina/config.yaml
```

Update the systemd unit's `ExecStart` to call this script.

### Azure Key Vault

Same pattern — use `az keyvault secret show` instead of `vault kv get`.

### SOPS for GitOps

Encrypt your env file with [SOPS](https://github.com/getsops/sops):

```bash
# Encrypt
sops --encrypt machina.env > machina.env.enc
git add machina.env.enc  # safe to commit

# Decrypt at deploy time
sops --decrypt machina.env.enc > /etc/machina/machina.env
chmod 600 /etc/machina/machina.env
```

## Custom Token Verifiers

For MCP auth specifically, a custom `TokenVerifier` can query an external
identity provider at runtime — no restart needed for token rotation. The
config accepts only verifier classes under the `machina.` package, so in v0.4
this means adding a module to the package. See
[MCP Auth](../mcp/auth.md#custom-token-verifiers) for details.

## What NOT to Do

- Do not commit `.env` files to version control
- Do not log secrets — on JSONL export, Machina redacts trace metadata whose
  key contains `token`, `password`, `secret`, `api_key`, `client_secret` or
  `authorization`, but only that metadata
- Do not pass secrets via CLI arguments (visible in `ps` output)
- Do not use the legacy `MACHINA_MCP_TOKENS` format — it lacks client identity tracking
