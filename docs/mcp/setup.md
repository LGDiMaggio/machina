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
llm:
  provider: "openai:gpt-4o"
sandbox: true
```

## Starting the Server

### stdio (default — for IDE integration)

```bash
python -m machina.mcp --config machina.yaml
```

Use this with Claude Desktop or Cursor. Add to your MCP client config:

```json
{
  "mcpServers": {
    "machina": {
      "command": "python",
      "args": ["-m", "machina.mcp", "--config", "/path/to/machina.yaml"]
    }
  }
}
```

!!! warning "Single-user only"
    stdio mode has no authentication. Any local process can connect.
    See [Security](../deployment/security.md) for details.

### streamable-http (for multi-client / server deployment)

```bash
python -m machina.mcp \
    --config machina.yaml \
    --transport streamable-http \
    --port 8000
```

Requires bearer token authentication. See [Auth](auth.md) for setup.

**CLI arguments:**

| Argument | Default | Description |
|----------|---------|-------------|
| `--config` | (required) | Path to machina.yaml |
| `--transport` | `stdio` | `stdio` or `streamable-http` |
| `--host` | `0.0.0.0` | Bind address (HTTP only) |
| `--port` | `8000` | Listen port (HTTP only) |

## Health Endpoint

The HTTP transport exposes `GET /health`:

```bash
# Unauthenticated — basic liveness check
curl http://localhost:8000/health
# {"status": "healthy", "server": "machina", "transport": "streamable-http"}

# Authenticated — includes connector details
curl -H "Authorization: Bearer <token>" http://localhost:8000/health
# {"status": "healthy", "connectors": {...}, "sandbox": true, "version": "0.3.0"}
```

## Lifecycle

On startup, the MCP server:

1. Loads config from YAML
2. Instantiates `MachinaRuntime` with all configured connectors
3. Calls `connect_all()` — establishes connections to CMMS, IoT, etc.
4. Auto-registers MCP tools based on connector capabilities
5. Registers resources and prompts

On shutdown (SIGTERM):

1. Stops accepting new requests
2. Drains in-flight requests (up to 30s)
3. Calls `disconnect_all()` on all connectors
4. Exits cleanly

## Docker

See [Docker Deployment](../deployment/docker.md) for the containerized setup.

## Next Steps

- [Tools](tools.md) — available MCP tools
- [Resources](resources.md) — queryable data resources
- [Prompts](prompts.md) — pre-built prompt templates
- [Auth](auth.md) — token configuration
