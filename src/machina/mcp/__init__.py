"""MCP Server layer — expose connectors as Model Context Protocol servers.

Status: not implemented. Planned for **v0.3**.

The MCP layer will expose every Machina connector as a `Model Context Protocol
<https://modelcontextprotocol.io/>`_ server, letting MCP-compatible clients
(Claude Desktop, Cursor, Continue, …) call connector capabilities as tools
without any agent code. See :mod:`machina.mcp.server` and ``docs/mcp-server.md``
for the planned design.

The ``machina.mcp`` namespace is reserved so that ``import machina.mcp`` keeps
working across the v0.2 → v0.3 transition. Concrete symbols raise
``NotImplementedError`` with a pointer to the roadmap until v0.3 lands.
"""

from __future__ import annotations

from machina.mcp.server import MCPServer

__all__ = ["MCPServer"]
