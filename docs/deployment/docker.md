# Docker Deployment

Run the Machina MCP server (streamable HTTP) in a container with Docker
Compose, next to a mock CMMS for a first run.

## Quick Start

```bash
cd deploy/docker
cp .env.example .env
# In .env, set MACHINA_MCP_TOKENS_JSON to a real token (see below)
docker compose up -d
```

This starts two services:

| Service | Host port | Description |
|---------|-----------|-------------|
| **machina** | `127.0.0.1:8000` | MCP server, streamable-HTTP transport (`/mcp`, `/health`) |
| **mock-cmms** | `127.0.0.1:9000` | Mock CMMS speaking the Generic CMMS REST contract, with in-memory sample data |

Both ports are published on the host's loopback interface only. The server
needs no LLM key: the MCP client brings its own model.

The server refuses to start until every bearer token is at least 32
characters, so the placeholder in `.env.example` does not work as a token.
Generate one per client:

```bash
openssl rand -hex 32
# .env:
# MACHINA_MCP_TOKENS_JSON={"<64-hex-char token>": "claude-desktop"}
```

## Verify

```bash
curl http://localhost:8000/health
# {"status":"healthy"}

curl -H "Authorization: Bearer <token>" http://localhost:8000/health
# {"status":"healthy","connectors":["cmms"],"sandbox_mode":true,"version":"0.4.0"}
```

Point an MCP client at `http://localhost:8000/mcp` with the
`Authorization: Bearer <token>` header (see [MCP Auth](../mcp/auth.md)).

## Architecture

```
docker-compose.yml
├── machina        ← MCP server (streamable HTTP on :8000)
│   ├── reads config.yaml (mounted read-only)
│   └── reads .env (tokens, CMMS URL and key, sandbox mode, log level)
└── mock-cmms      ← Fake CMMS REST API (:9000), health-checked before machina starts
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MACHINA_MCP_TOKENS_JSON` | Yes | `{"<token>": "<client_id>"}`; every token ≥ 32 characters |
| `MACHINA_CMMS_URL` | No | CMMS REST base URL; defaults to the mock CMMS |
| `MACHINA_CMMS_API_KEY` | No | CMMS bearer token; defaults to a demo value the mock CMMS accepts |
| `MACHINA_SANDBOX_MODE` | No | `true` (default): writes are logged and answered with a `[SANDBOX]` result |
| `MACHINA_LOG_LEVEL` | No | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

The CMMS, sandbox and log-level variables take effect because
`config.yaml` references them as `${VAR}` placeholders; edit the config to
wire more.

### Config File

`config.yaml` is mounted read-only into the container. It configures a
`generic_cmms` connector in REST mode with the optional work-order,
maintenance-plan, spare-part and maintenance-history endpoints, so the server
offers 11 tools. Edit it to add
connectors (Excel, SQL, a document store, a vendor CMMS); see
[YAML Configuration](../yaml-config.md) and the [MCP setup](../mcp/setup.md).

To reach a real CMMS, set `MACHINA_CMMS_URL` and `MACHINA_CMMS_API_KEY` in
`.env` (or change the connector in `config.yaml`) and remove the
`mock-cmms` service and the `depends_on` block from `docker-compose.yml`.

## Docker Image

The Dockerfile (`deploy/docker/Dockerfile`) uses a multi-stage build:

- **Base image:** `python:3.11-slim-bookworm`
- **Extras installed:** `[cmms-rest,mcp]` by default — set the
  `MACHINA_EXTRAS` build argument for more, e.g.
  `docker compose build --build-arg MACHINA_EXTRAS=cmms-rest,mcp,docs-rag,excel`
- **User:** non-root `machina`
- **Healthcheck:** `GET /health` every 30 s
- **Entrypoint:** `machina mcp serve --transport streamable-http --host 0.0.0.0 --port 8000 --config /home/machina/config.yaml`

### Building Locally

The build context is the repository root:

```bash
docker build -f deploy/docker/Dockerfile -t machina:latest .
```

### Pinning the Base Image

For production, pin the base image digests:

```dockerfile
FROM python:3.11-slim-bookworm@sha256:<digest> AS build
```

## Mock CMMS

The mock CMMS (`deploy/docker/mock-cmms/`) is a FastAPI app with in-memory
data — 3 assets, 4 work orders (2 of them completed history), 3 spare parts
and a maintenance plan — that resets on restart. It requires a bearer token
and accepts any non-empty one.

| Method | Path | Used by |
|--------|------|---------|
| `GET` | `/health` | connector `connect()` |
| `GET` | `/assets`, `/assets/{id}` | `read_assets` |
| `GET` | `/work_orders?asset_id=&status=` | `read_work_orders` |
| `POST` | `/work_orders` | `create_work_order` (idempotent on `id`) |
| `GET` | `/work_orders/{id}` | `get_work_order` endpoint |
| `PATCH` | `/work_orders/{id}` | `update_work_order` endpoint (also close and cancel) |
| `GET` | `/maintenance_plans` | `read_maintenance_plans` endpoint |
| `GET` | `/spare_parts?asset_id=&sku=` | `read_spare_parts` endpoint |
| `GET` | `/assets/{id}/history` | `read_maintenance_history` endpoint (the asset's completed and closed work orders) |

Without its endpoint, the Generic CMMS connector in REST mode does not offer
the matching tool — see [Generic CMMS](../connectors/generic_cmms.md).

## Logs

```bash
docker compose logs -f machina
```

Action traces are not written to disk by the MCP server; see
[Action Traces](../observability/traces.md) for exporting them from your own
code.

## Stopping

```bash
docker compose down
```
