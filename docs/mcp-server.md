# MCP Server

The **MCP Server layer** will expose every Machina connector as a
[Model Context Protocol](https://modelcontextprotocol.io/) server, letting
**Claude Desktop**, **Cursor**, and any MCP-compatible client use Machina's
CMMS, document-store, and messaging connectors without writing a single line
of agent code. You point your MCP client at a Machina server, and your
connectors' capabilities become tools the client can call directly.

!!! warning "Not available yet"
    The MCP layer is a **placeholder**. The `machina.mcp` namespace is
    importable so the import path stays stable across v0.2 → v0.3, but
    instantiating `machina.mcp.MCPServer` raises `NotImplementedError`
    with a pointer back here. The full implementation is planned for
    **v0.3** — see the [Roadmap](roadmap.md). Until then, use the
    `Agent` class directly — see the [Quickstart](quickstart.md).

## Why MCP?

- **Adoption multiplier.** Every MCP client (Claude Desktop, Cursor, Continue,
  Cline, …) immediately gains the ability to talk to any CMMS, document store,
  or communication channel Machina supports — no integration work required.
- **No-code integration.** Users who want a maintenance assistant but don't want
  to write Python can spin up a Machina MCP server and point their existing LLM
  client at it. The agent layer becomes optional.

## How it will work

The MCP layer is a **thin protocol adapter** on top of the existing connector
layer, not a separate system. When a connector is instantiated and registered,
its declared `capabilities` are automatically mapped to MCP tool definitions:

| Connector capability | MCP tool |
|---|---|
| `read_assets` | `list_assets`, `get_asset_details` |
| `read_work_orders` | `list_work_orders`, `filter_work_orders` |
| `create_work_order` | `create_work_order` |
| `search_documents` | `search_manuals` |
| `read_spare_parts` | `check_inventory` |

The mapping is configured once in `src/machina/mcp/tools.py`; adding a new
capability to a connector automatically exposes it as an MCP tool — no manual
registration.

## See also

- **[Custom Connectors](connectors/custom.md)** — How to build a connector
  that will be exposed via MCP once the layer ships
- **[Architecture](architecture.md)** — Where the MCP layer sits in the
  five-layer stack
- **[MACHINA_SPEC §17](https://github.com/LGDiMaggio/machina/blob/main/MACHINA_SPEC.md#17-mcp-server-layer)** —
  Full spec for the MCP Server layer
