# Docker Deployment

Run Machina as a containerized MCP server with Docker Compose.

## Quick Start

```bash
cd deploy/docker
cp .env.example .env
# Edit .env with your LLM key, CMMS credentials, MCP tokens
docker compose up -d
```

This starts three services:

| Service | Port | Description |
|---------|------|-------------|
| **machina** | 8000 | MCP server (streamable-http transport) |
| **chromadb** | 8001 | RAG vector store |
| **mock-cmms** | 9000 | Mock CMMS with hardcoded sample data |

## Verify

```bash
curl http://localhost:8000/health
# {"status": "healthy", "server": "machina", "transport": "streamable-http"}
```

## Architecture

```
docker-compose.yml
├── machina        ← MCP server (streamable-http on :8000)
│   ├── reads config.yaml (mounted read-only)
│   ├── reads .env for secrets
│   └── writes traces to machina-traces volume
├── chromadb       ← Vector store for RAG (:8001)
│   └── persists to chromadb-data volume
└── mock-cmms      ← Fake CMMS API (:9000)
    └── Hardcoded assets, work orders, spare parts
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `MACHINA_LLM_MODEL` | Yes | LiteLLM model ID (e.g., `openai/gpt-4o`) |
| `OPENAI_API_KEY` | Yes* | API key for your LLM provider |
| `MACHINA_MCP_TOKENS_JSON` | Yes (HTTP) | Token-to-client mapping for auth |
| `MACHINA_CMMS_URL` | No | CMMS URL (defaults to mock-cmms) |
| `MACHINA_CMMS_TYPE` | No | Connector type (defaults to `generic_cmms`) |
| `MACHINA_SANDBOX_MODE` | No | `true` for safe experimentation |
| `MACHINA_LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

*Or `ANTHROPIC_API_KEY` etc. depending on `MACHINA_LLM_MODEL`.

### Config File

The Machina config (`config.yaml`) is mounted read-only into the container.
Edit it for connector settings, channels, and agent behavior. See
[YAML Configuration](../yaml-config.md).

## Docker Image

The Dockerfile (`deploy/docker/Dockerfile`) uses a multi-stage build:

- **Base image:** `python:3.11-slim-bookworm`
- **Extras installed:** `[cmms-rest,litellm,docs-rag,mcp]`
- **User:** Non-root `machina`
- **Healthcheck:** `GET /health` every 30s
- **Entrypoint:** `python -m machina.mcp --transport streamable-http`

### Building Locally

```bash
cd deploy/docker
docker build -t machina:latest .
```

### Pinning the Base Image

For production, pin the base image digest:

```dockerfile
FROM python:3.11-slim-bookworm@sha256:<digest> AS builder
```

## Volumes

| Volume | Purpose | Backup? |
|--------|---------|---------|
| `machina-traces` | Action trace JSONL files | Recommended |
| `chromadb-data` | RAG embeddings | Rebuild from source docs if lost |

## Mock CMMS

The included mock CMMS (`deploy/docker/mock-cmms/`) is a FastAPI app with
hardcoded data — 3 assets, 2 work orders, spare parts, and maintenance plans.

Endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/assets` | List assets |
| `GET` | `/api/assets/{id}` | Get asset by ID |
| `GET` | `/api/work-orders` | List work orders |
| `POST` | `/api/work-orders` | Create work order (echoes payload) |
| `GET` | `/api/spare-parts` | List spare parts |
| `GET` | `/api/maintenance-plans` | List plans |
| `GET` | `/health` | Health check |

Replace with your real CMMS by changing `MACHINA_CMMS_URL` and `MACHINA_CMMS_TYPE`
in `.env`.

## Logs

```bash
# Follow Machina logs
docker compose logs -f machina

# View trace files
docker compose exec machina ls /home/machina/traces/

# Copy traces to host
docker compose cp machina:/home/machina/traces/ ./traces/
```

## Stopping

```bash
docker compose down          # Stop services, keep volumes
docker compose down -v       # Stop and remove volumes (deletes traces + embeddings)
```
