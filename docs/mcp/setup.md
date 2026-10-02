# MCP Server Setup

Machina exposes its connectors as an [MCP](https://modelcontextprotocol.io) server,
allowing Claude Desktop, Cursor, and any MCP-compatible client to interact with your
maintenance data without writing agent code.

## Install

```bash
pip install "machina-ai[mcp]"
```

## Configuration

Create a `machina.yaml` config file (see [YAML Configuration](../yaml-config.md)):

```yaml
name: "Maintenance MCP Server"
plant:
  name: "My Plant"
connectors:
  cmms:
    type: generic_cmms
    primary: true
    settings:
      data_dir: "./sample_data/cmms"
sandbox: true
```

The server makes no LLM calls — the MCP client brings its own model — so no
`llm:` section is needed. `logging: {level: DEBUG}` raises the server's log
level (logs go to stderr on stdio, stdout on HTTP).

## Starting the Server

`machina mcp serve` and `python -m machina.mcp` are the same command with the
same options.

### stdio (default — for desktop and IDE clients)

```bash
machina mcp serve --config machina.yaml
```

The client launches the server itself. Add to your MCP client config:

```json
{
  "mcpServers": {
    "machina": {
      "command": "machina",
      "args": ["mcp", "serve", "--config", "/path/to/machina.yaml"]
    }
  }
}
```

(Equivalently `"command": "python", "args": ["-m", "machina.mcp", "--config", "/path/to/machina.yaml"]`.)

!!! warning "Single-user only"
    stdio mode has no authentication: the client that launches the process
    owns it. See [Security](../deployment/security.md) for details.

### streamable-http (for multi-client / server deployment)

```bash
export MACHINA_MCP_TOKENS_JSON='{"<64-hex-char token>": "ops-dashboard"}'
machina mcp serve \
    --config machina.yaml \
    --transport streamable-http \
    --port 8000
```

Requires bearer token authentication; tokens must be at least 32 characters
(`openssl rand -hex 32`). See [Auth](auth.md) for tokens and for the Host /
Origin allow-lists. The server listens on `127.0.0.1` unless you pass
`--host 0.0.0.0` (needed inside a container, or behind a reverse proxy on
another machine). Clients connect to `http://<host>:<port>/mcp`.

**CLI arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `--config` | (required) | Path to machina.yaml |
| `--transport` | `stdio` | `stdio` or `streamable-http` |
| `--host` | `127.0.0.1` | Bind address (HTTP only) |
| `--port` | `8000` | Listen port (HTTP only) |

## Health Endpoint

The HTTP transport serves `GET /health`. It needs no token, so container
health checks can call it; a request with a valid bearer token gets details:

```bash
# Unauthenticated — liveness only
curl http://localhost:8000/health
# {"status": "healthy"}

# Authenticated — connector names, sandbox mode, package version
curl -H "Authorization: Bearer <token>" http://localhost:8000/health
# {"status": "healthy", "connectors": ["cmms"], "sandbox_mode": true, "version": "0.4.0"}
```

## Lifecycle

On startup the server loads and validates the config, builds a
`MachinaRuntime` with the configured connectors, connects them
(`connect_all()`), and registers the tools allowed by their capabilities,
plus the resources and prompts. A connector that fails to connect is logged
and skipped; the others keep serving.

- **stdio** — the runtime lives for the client session (one per process).
- **streamable-http** — one runtime per server process: connectors connect
  once at startup and every request shares them. Requests are handled
  statelessly, so any instance behind a load balancer can serve any request.

On shutdown the HTTP server stops accepting connections, lets in-flight
requests finish, and then disconnects every connector (`disconnect_all()`).

## Docker

See [Docker Deployment](../deployment/docker.md) for the containerized setup.

## Next Steps

- [Tools](tools.md) — available MCP tools
- [Resources](resources.md) — queryable data resources
- [Prompts](prompts.md) — pre-built prompt templates
- [Auth](auth.md) — token configuration
