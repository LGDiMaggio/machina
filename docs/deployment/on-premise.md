# On-Premise Deployment

This guide covers running Machina on your own infrastructure — either as a systemd service
directly on a Linux host, or via Docker Compose.

## Choosing a Deployment Method

| Method | Best for | Requires |
|--------|----------|----------|
| **systemd** | Single-host production, air-gapped sites, minimal footprint | Linux host, Python 3.11+ |
| **Docker Compose** | Quick evaluation, multi-service stack (Machina + ChromaDB + mock CMMS) | Docker Engine |

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
sudo /opt/machina-venv/bin/pip install "machina-ai[cmms-rest,litellm,docs-rag,mcp]"
```

### 3. Configure

Copy the environment file and your YAML config:

```bash
sudo cp deploy/systemd/machina.env.example /etc/machina/machina.env
sudo chmod 600 /etc/machina/machina.env
# Edit with your secrets: LLM key, CMMS credentials, MCP tokens
sudo vi /etc/machina/machina.env

# Place your machina config.yaml
sudo cp your-config.yaml /etc/machina/config.yaml
```

!!! warning "Secrets"
    `/etc/machina/machina.env` contains API keys and CMMS credentials.
    Keep it `chmod 600` and owned by `root`. systemd reads it before dropping
    privileges to the `machina` user.

### 4. Install and start the service

```bash
sudo cp deploy/systemd/machina.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now machina
```

### 5. Verify

```bash
sudo systemctl status machina
curl -s http://localhost:8000/health | python3 -m json.tool
```

## Docker Compose

See `deploy/docker/README.md` for the full Docker walkthrough. Quick start:

```bash
cd deploy/docker
cp .env.example .env
# Edit .env with your secrets
docker compose up -d
curl http://localhost:8000/health
```

## Log Locations

| Source | systemd path | Docker |
|--------|-------------|--------|
| Application log | `/var/log/machina/machina.log` | `docker compose logs machina` |
| Error log | `/var/log/machina/machina.err` | (merged into stdout) |
| Action traces (JSONL) | `/var/lib/machina/traces/` | `machina-traces` volume |
| journald | `journalctl -u machina` | N/A |

### Reading trace files

Trace files are JSONL — one JSON object per line, one file per conversation:

```bash
# List recent traces
ls -lt /var/lib/machina/traces/ | head

# Pretty-print a trace
python3 -m json.tool /var/lib/machina/traces/2026-04-20_abc123.jsonl

# Search for a specific tool call
grep '"tool_name": "create_work_order"' /var/lib/machina/traces/*.jsonl
```

## Upgrade Procedure

### systemd

```bash
sudo systemctl stop machina
sudo /opt/machina-venv/bin/pip install --upgrade "machina-ai[cmms-rest,litellm,docs-rag,mcp]"
sudo systemctl start machina
sudo journalctl -u machina -n 50 --no-pager  # verify clean startup
```

### Docker

```bash
cd deploy/docker
docker compose pull
docker compose up -d
docker compose logs -f machina  # verify clean startup
```

## Network Requirements

Machina needs outbound access to:

| Destination | Port | Purpose |
|-------------|------|---------|
| LLM provider API (e.g. `api.openai.com`) | 443 | LLM inference |
| CMMS host | Varies | Read assets, create work orders |
| ChromaDB (if remote) | 8000 | RAG vector store |
| OPC-UA server (if configured) | 4840 | Sensor data |
| MQTT broker (if configured) | 1883/8883 | IoT telemetry |

Machina listens on:

| Port | Transport | Auth |
|------|-----------|------|
| 8000 (configurable) | streamable-http | Static bearer token |
| N/A | stdio | Implicit (local process) |
