# On-Premise Deployment

This guide covers running the Machina MCP server on your own infrastructure —
either as a systemd service directly on a Linux host, or via Docker Compose.

## Choosing a Deployment Method

| Method | Best for | Requires |
|--------|----------|----------|
| **systemd** | Single-host production, air-gapped sites, minimal footprint | Linux host, Python 3.11+ |
| **Docker Compose** | Quick evaluation (Machina + a mock CMMS) | Docker Engine |

## Systemd (Bare Metal)

### 1. Create the machina user

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/machina machina
sudo mkdir -p /var/lib/machina /var/log/machina /etc/machina
sudo chown machina:machina /var/lib/machina /var/log/machina
```

### 2. Install Machina

```bash
sudo python3.11 -m venv /opt/machina-venv
sudo /opt/machina-venv/bin/pip install "machina-ai[cmms-rest,mcp]"
```

Add the extras your connectors need (`docs-rag` for manuals, `excel`, `sql`,
`sap`, …); the MCP server itself makes no LLM calls.

### 3. Configure

Copy the environment file and a YAML config. `deploy/docker/config.yaml` is a
good starting point: it reads the CMMS URL and key, sandbox mode and log level
from the environment.

```bash
sudo cp deploy/systemd/machina.env.example /etc/machina/machina.env
sudo chmod 600 /etc/machina/machina.env
# Set the MCP tokens (>= 32 characters each), the CMMS URL and key
sudo vi /etc/machina/machina.env

sudo cp deploy/docker/config.yaml /etc/machina/config.yaml
```

!!! warning "Secrets"
    `/etc/machina/machina.env` contains MCP tokens and CMMS credentials.
    Keep it `chmod 600` and owned by `root`. systemd reads it before dropping
    privileges to the `machina` user.

### 4. Install and start the service

```bash
sudo cp deploy/systemd/machina.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now machina
```

The unit runs `machina mcp serve --transport streamable-http` on
`127.0.0.1:8000`.

### 5. Verify

```bash
sudo systemctl status machina
curl -s http://127.0.0.1:8000/health
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8000/health
```

## Docker Compose

See [Docker Deployment](docker.md) for the full walkthrough. Quick start:

```bash
cd deploy/docker
cp .env.example .env
# Set MACHINA_MCP_TOKENS_JSON in .env
docker compose up -d
curl http://localhost:8000/health
```

## Log Locations

| Source | systemd | Docker |
|--------|---------|--------|
| Application log | `/var/log/machina/machina.log` | `docker compose logs machina` |
| Server errors | `/var/log/machina/machina.err` | (merged into the same stream) |
| Service lifecycle | `journalctl -u machina` | `docker compose ps` |

The MCP server does not write action traces to disk. To keep traces from an
agent you run yourself, attach a JSONL exporter — see
[Action Traces](../observability/traces.md).

## Upgrade Procedure

### systemd

```bash
sudo systemctl stop machina
sudo /opt/machina-venv/bin/pip install --upgrade "machina-ai[cmms-rest,mcp]"
sudo systemctl start machina
tail -n 50 /var/log/machina/machina.log  # verify clean startup
```

### Docker

```bash
cd deploy/docker
git pull                      # the image is built from the repository
docker compose up -d --build
docker compose logs -f machina  # verify clean startup
```

## Network Requirements

The MCP server needs outbound access to the systems its connectors use:

| Destination | Port | Purpose |
|-------------|------|---------|
| CMMS host | Varies | Read assets, create and update work orders |
| OPC-UA server (if configured) | 4840 | Sensor data |
| MQTT broker (if configured) | 1883/8883 | IoT telemetry |

An agent you run yourself (not the MCP server) also needs your LLM provider
(e.g. `api.openai.com:443`), unless it uses a local model.

The server listens on:

| Port | Transport | Auth |
|------|-----------|------|
| 8000 (configurable, loopback by default) | streamable-http | Static bearer token |
| N/A | stdio | Implicit (local process) |
