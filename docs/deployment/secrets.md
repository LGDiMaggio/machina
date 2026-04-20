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
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM provider | Restart required |
| `MACHINA_MCP_TOKENS_JSON` | MCP client auth | Restart; coordinate with clients |
| `MACHINA_CMMS_USERNAME` / `MACHINA_CMMS_PASSWORD` | CMMS connector | Restart required |
| OPC-UA certificates | IoT connector | Restart; re-create subscriptions |
| `MACHINA_TELEGRAM_BOT_TOKEN` | Telegram connector | Restart required |
| SMTP/IMAP credentials | Email connector | Restart required |

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

Docker Compose loads `.env` automatically. Add `.env` to `.gitignore`.

## Advanced: External Secret Stores

### HashiCorp Vault

Create a wrapper script that fetches secrets and execs the server:

```bash
#!/bin/bash
export OPENAI_API_KEY=$(vault kv get -field=key secret/machina/openai)
export MACHINA_CMMS_PASSWORD=$(vault kv get -field=password secret/machina/cmms)
export MACHINA_MCP_TOKENS_JSON=$(vault kv get -field=tokens secret/machina/mcp)
exec /opt/machina-venv/bin/python -m machina.mcp \
    --transport streamable-http \
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

For MCP auth specifically, you can implement a custom `TokenVerifier` that
queries an external identity provider at runtime — no restart needed for
token rotation. See [MCP Auth](../mcp/auth.md) for details.

## What NOT to Do

- Do not commit `.env` files to version control
- Do not log secrets — Machina's `ActionTracer` redacts fields matching
  `token`, `password`, `secret`, `api_key` patterns automatically
- Do not pass secrets via CLI arguments (visible in `ps` output)
- Do not use the legacy `MACHINA_MCP_TOKENS` format — it lacks client identity tracking
