"""MCP Server layer — expose connectors as Model Context Protocol servers.

Status: not implemented. Planned for **v0.3**.

The MCP layer will expose every Machina connector as a `Model Context Protocol
<https://modelcontextprotocol.io/>`_ server, letting MCP-compatible clients
(Claude Desktop, Cursor, Continue, …) call connector capabilities as tools
without any agent code. See ``docs/roadmap.md`` and ``docs/mcp-server.md`` for
the planned design.

The ``machina.mcp`` namespace is reserved so that ``import machina.mcp`` keeps
working across the v0.2 → v0.3 transition. Concrete symbols raise
``NotImplementedError`` with a pointer to the roadmap until v0.3 lands.
"""

from __future__ import annotations

__all__ = ["MCPServer"]

_ROADMAP_MESSAGE = (
    "MCP server is planned for v0.3. See docs/roadmap.md (or docs/mcp-server.md) for status."
)


class MCPServer:
    """Placeholder for the v0.3 MCP server.

    Instantiation raises :class:`NotImplementedError` so that any code reaching
    for the server today fails loudly with a roadmap pointer instead of
    silently importing an empty namespace.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(_ROADMAP_MESSAGE)
