# MCP Server

Machina exposes its connectors as a [Model Context Protocol](https://modelcontextprotocol.io/)
server, so **Claude Desktop**, **Cursor** and any other MCP client can read
and write maintenance data through Machina's connectors without agent code.
The client brings its own LLM; the server makes no LLM calls.

The layer is a thin protocol adapter (FastMCP from the MCP Python SDK) over
the connector layer: every capability a configured connector declares turns
on the matching tools, so the server offers exactly what its connectors can
serve.

```bash
pip install "machina-ai[mcp]"
machina mcp serve --config machina.yaml        # same as: python -m machina.mcp --config machina.yaml
```

## What it serves

| Surface | Contents |
|---------|----------|
| [Tools](mcp/tools.md) | 15 domain tools (`machina_list_assets`, `machina_create_work_order`, `machina_search_manuals`, …), registered per declared capability; 2 opt-in vendor tools (`enable_vendor_tools`) |
| [Resources](mcp/resources.md) | 4 versioned resources: `machina://v1/assets/{asset_id}`, `machina://v1/work-orders/{wo_id}`, `machina://v1/failure-taxonomy`, `machina://v1/capabilities` |
| [Prompts](mcp/prompts.md) | 3 templates: `diagnose_asset_failure`, `draft_preventive_plan`, `summarize_maintenance_history` |

## Transports

| Transport | Use | Auth |
|-----------|-----|------|
| `stdio` (default) | One local client (Claude Desktop, an IDE) launching the server | None — the client owns the process |
| `streamable-http` | Multi-client / server deployment; also serves `GET /health` | Static bearer tokens (≥ 32 characters) |

Writes go through the connectors' `@sandbox_aware` guard: with `sandbox: true`
in the config, write tools return a marked `[SANDBOX]` result and nothing
reaches the CMMS.

## Read next

- [Setup](mcp/setup.md) — configuration, both transports, client configuration, `/health`
- [Auth](mcp/auth.md) — bearer tokens, allowed hosts and origins
- [Tools](mcp/tools.md) · [Resources](mcp/resources.md) · [Prompts](mcp/prompts.md)
- [Docker deployment](deployment/docker.md) — the containerized HTTP server
