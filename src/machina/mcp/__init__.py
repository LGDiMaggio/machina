"""MCP Server layer — expose connectors as Model Context Protocol servers.

The MCP server uses FastMCP to expose Machina connectors as MCP tools,
resources and prompts.  Build a server with :func:`build_server`, or run
one with :func:`serve` (what ``machina mcp serve`` and
``python -m machina.mcp`` call).

The legacy ``MCPServer`` placeholder is deprecated — accessing it
emits a :class:`DeprecationWarning`.
"""

from __future__ import annotations

import warnings
from typing import Any

from machina.mcp.server import build_server, serve

__all__ = ["build_server", "serve"]


def __getattr__(name: str) -> Any:
    if name == "MCPServer":
        warnings.warn(
            "MCPServer is deprecated and was removed in v0.3. "
            "Use machina.mcp.build_server(config) instead. "
            "See docs/migration/v0.2-to-v0.3.md for details.",
            DeprecationWarning,
            stacklevel=2,
        )
        raise AttributeError("MCPServer was removed in v0.3. Use build_server(config) instead.")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
