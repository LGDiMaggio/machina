# Systemd Deployment

Run the Machina MCP server (streamable HTTP) as a systemd service on a Linux host.

## Prerequisites

- Python 3.11+ installed (system package or pyenv)
- A dedicated `machina` user/group
- Network access to your CMMS (the MCP server makes no LLM calls)

## Install

```bash
# Create user and directories
sudo useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/machina machina
sudo mkdir -p /var/lib/machina /var/log/machina /etc/machina
sudo chown machina:machina /var/lib/machina /var/log/machina

# Create virtualenv and install (add the extras your connectors need,
# e.g. docs-rag for manuals, excel, sql, sap)
sudo python3.11 -m venv /opt/machina-venv
sudo /opt/machina-venv/bin/pip install "machina-ai[cmms-rest,mcp]"

# Configuration: start from the Docker config, which reads the CMMS URL,
# the CMMS key, sandbox mode and log level from the environment
sudo cp ../docker/config.yaml /etc/machina/config.yaml
sudo cp machina.env.example /etc/machina/machina.env
sudo chmod 600 /etc/machina/machina.env
# Edit /etc/machina/machina.env: MCP tokens (>= 32 characters each),
# CMMS URL and key

# Install and start the service
sudo cp machina.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now machina
```

The unit listens on `127.0.0.1:8000`. Put a TLS-terminating reverse proxy in
front of it for clients on other machines, and add the public host name to
`mcp.allowed_hosts` in the config (see the MCP authentication docs).

## Logs

The unit appends the server's output to files; the journal records only
service starts, stops and failures.

```bash
tail -f /var/log/machina/machina.log   # structured application logs and HTTP access log
tail -f /var/log/machina/machina.err   # server startup messages and errors
sudo journalctl -u machina -n 50       # service lifecycle
```

## Upgrade

```bash
sudo systemctl stop machina
sudo /opt/machina-venv/bin/pip install --upgrade "machina-ai[cmms-rest,mcp]"
sudo systemctl start machina
tail -n 50 /var/log/machina/machina.log  # verify clean startup
```

## Troubleshooting

```bash
# Check service status
sudo systemctl status machina

# Verify unit file syntax
systemd-analyze verify /etc/systemd/system/machina.service

# Liveness, then details with a token
curl http://127.0.0.1:8000/health
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/health

# Run the server in the foreground as the machina user, with the unit's
# environment file (stop the service first: the port is shared)
sudo systemctl stop machina
sudo systemd-run --pty --uid=machina --gid=machina \
    -p EnvironmentFile=/etc/machina/machina.env \
    /opt/machina-venv/bin/machina mcp serve --transport streamable-http \
    --host 127.0.0.1 --port 8000 --config /etc/machina/config.yaml
```
