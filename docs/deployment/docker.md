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
`generic_cmms` connector in REST mode with the optional work-order and
maintenance-plan endpoints, so the server offers 11 tools. Edit it to add
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

An image you build contains third-party software. If you distribute it,
see [Redistributing Images](redistribution.md) for what comes with it.

### Building Locally

The build context is the repository root:

```bash
docker build -f deploy/docker/Dockerfile -t machina:latest .
```

### Build Arguments

Every build argument is optional. With none set, both images install
exactly what they installed before these arguments existed.

| Argument | Images | Default | Effect when set |
|----------|--------|---------|-----------------|
| `MACHINA_EXTRAS` | `machina` | `cmms-rest,mcp` | The extras installed with Machina |
| `PYTHON_BASE` | both | `python:3.11-slim-bookworm` | The base image of every stage, for example one pinned by digest |
| `MACHINA_CONSTRAINTS` | both | empty | A pip constraints file. `constraints.txt` pins every install to the versions committed next to the Dockerfile |
| `TORCH_INDEX_URL` | `machina` | empty | Installs `torch` and `torchvision` from that index before the extras. For the RAG extras only |

A `docker compose build --build-arg …` applies the argument to both
services; a `docker build` applies it to the one image.

**Pinned Python dependencies.** `deploy/docker/constraints.txt` and
`deploy/docker/mock-cmms/constraints.txt` hold exact versions, so two builds
on different days install the same packages:

```bash
docker compose build --build-arg MACHINA_CONSTRAINTS=constraints.txt
```

The machina file covers every runtime extra (`all` plus `sql-jdbc`), so
whatever `MACHINA_EXTRAS` you choose is pinned. pip applies a constraint only
to packages it installs; the rest of the file has no effect. Both files are
resolved for Python 3.11 on every platform (amd64 and arm64 alike).

**Pinned base image.** Pass a digest instead of a tag:

```bash
docker build -f deploy/docker/Dockerfile \
  --build-arg PYTHON_BASE=python:3.11-slim-bookworm@sha256:<digest> -t machina:latest .
```

`docker buildx imagetools inspect python:3.11-slim-bookworm` prints the
current digest. A base image with another Python version needs constraints
files resolved for that version.

**CPU-only torch.** On Linux, on both x86_64 and arm64, the RAG extras
(`docs-rag-rerank`, `docs-rag-parsing`, `docs-rag-pro`, `all`) pull the CUDA
build of `torch` and NVIDIA's CUDA wheels from PyPI. To leave them out, install `torch` and
`torchvision` from the PyTorch CPU index:

```bash
docker build -f deploy/docker/Dockerfile \
  --build-arg MACHINA_EXTRAS=cmms-rest,mcp,docs-rag-pro \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu \
  -t machina:rag-cpu .
```

Both packages come from that index alone (`--index-url`, never
`--extra-index-url`, which could take the CUDA build or another package
from the wrong index). They are installed without their dependencies, which
then resolve from PyPI with the extras. They are installed together because
torchvision's compiled operators are built for one torch variant.

**Regenerating the constraints files.** uv records each file's command in
its header. Run them from the repository root with uv 0.12.22:

```bash
uv pip compile --universal --python-version 3.11 --extra all --extra sql-jdbc pyproject.toml -o deploy/docker/constraints.txt
printf 'fastapi\nuvicorn\n' | uv pip compile --universal --python-version 3.11 - -o deploy/docker/mock-cmms/constraints.txt
```

## Mock CMMS

The mock CMMS (`deploy/docker/mock-cmms/`) is a FastAPI app with in-memory
data — 3 assets, 2 work orders and a maintenance plan — that resets on
restart. It requires a bearer token and accepts any non-empty one.

| Method | Path | Used by |
|--------|------|---------|
| `GET` | `/health` | connector `connect()` |
| `GET` | `/assets`, `/assets/{id}` | `read_assets` |
| `GET` | `/work_orders?asset_id=&status=` | `read_work_orders` |
| `POST` | `/work_orders` | `create_work_order` (idempotent on `id`) |
| `GET` | `/work_orders/{id}` | `get_work_order` endpoint |
| `PATCH` | `/work_orders/{id}` | `update_work_order` endpoint (also close and cancel) |
| `GET` | `/maintenance_plans` | `read_maintenance_plans` endpoint |

In REST mode the Generic CMMS connector does not fetch spare parts or
maintenance history: those two tools return empty lists. See
[Generic CMMS](../connectors/generic_cmms.md).

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
