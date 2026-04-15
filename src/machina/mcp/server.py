"""Placeholder home for the v0.3 MCP server implementation.

When the real MCP layer ships, the production ``MCPServer`` class lives here
(per ``MACHINA_SPEC.md`` §17 and the project structure in ``CLAUDE.md``).
For v0.2.x this module exposes a loud stub so that ``import machina.mcp``
keeps working but instantiation fails with a clear pointer to the roadmap.
"""

from __future__ import annotations

__all__ = ["MCPServer"]

_ROADMAP_MESSAGE = (
    "MCP server is planned for v0.3. "
    "See https://github.com/LGDiMaggio/machina/blob/main/docs/mcp-server.md "
    "for the roadmap."
)


class MCPServer:
    """Placeholder for the v0.3 MCP server.

    Instantiation raises :class:`NotImplementedError` so that any code reaching
    for the server today fails loudly with a roadmap pointer instead of
    silently importing an empty namespace. The real signature is intentionally
    undefined here — accept no arguments so misuse fails fast as ``TypeError``
    rather than being swallowed by a permissive ``*args, **kwargs`` shim.

    Callers must catch :class:`NotImplementedError` directly to detect the
    placeholder; this is a stdlib exception and does not inherit from
    :class:`machina.exceptions.MachinaError` by design.
    """

    def __init__(self) -> None:
        raise NotImplementedError(_ROADMAP_MESSAGE)
