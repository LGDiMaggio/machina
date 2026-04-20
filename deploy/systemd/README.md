# Systemd Deployment

Run Machina as a systemd service on a Linux host.

## Prerequisites

- Python 3.11+ installed (system package or pyenv)
- A dedicated `machina` user/group
- Network access to your CMMS and LLM provider

## Install

```bash
# Create user and directories
sudo useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/machina machina
sudo mkdir -p /var/lib/machina /var/log/machina /etc/machina
sudo chown machina:machina /var/lib/machina /var/log/machina

# Create virtualenv and install
sudo python3.11 -m venv /opt/machina-venv
sudo /opt/machina-venv/bin/pip install "machina-ai[cmms-rest,litellm,docs-rag,mcp]"

# Copy configuration
sudo cp machina.env.example /etc/machina/machina.env
sudo chmod 600 /etc/machina/machina.env
# Edit /etc/machina/machina.env with your secrets

# Copy your machina config.yaml to /etc/machina/config.yaml

# Install and start the service
sudo cp machina.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now machina
```

## Logs

```bash
# Structured log output
sudo journalctl -u machina -f

# Or read log files directly
tail -f /var/log/machina/machina.log
tail -f /var/log/machina/machina.err

# Trace files (JSONL action traces)
ls /var/lib/machina/traces/
```

## Upgrade

```bash
sudo systemctl stop machina
sudo /opt/machina-venv/bin/pip install --upgrade "machina-ai[cmms-rest,litellm,docs-rag,mcp]"
sudo systemctl start machina
sudo journalctl -u machina -n 50 --no-pager  # verify clean startup
```

## Troubleshooting

```bash
# Check service status
sudo systemctl status machina

# Verify unit file syntax
systemd-analyze verify /etc/systemd/system/machina.service

# Test the command manually as the machina user
sudo -u machina /opt/machina-venv/bin/python -m machina.mcp \
    --transport streamable-http \
    --config /etc/machina/config.yaml
```
