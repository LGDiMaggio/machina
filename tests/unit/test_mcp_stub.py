"""Contract tests for the ``machina.mcp`` v0.3 placeholder.

The module is intentionally a loud stub: ``import machina.mcp`` must keep
working (semver — the namespace is reserved across the v0.2 → v0.3 jump),
but instantiating any concrete symbol must raise ``NotImplementedError``
with a roadmap pointer so callers don't silently get an empty namespace.
"""

from __future__ import annotations

import pytest


class TestMcpStub:
    def test_import_namespace_succeeds(self) -> None:
        import machina.mcp  # noqa: F401 — the import itself is the assertion

    def test_mcpserver_instantiation_raises_with_roadmap_pointer(self) -> None:
        from machina.mcp import MCPServer

        with pytest.raises(NotImplementedError) as exc_info:
            MCPServer()

        message = str(exc_info.value)
        assert "v0.3" in message
        assert "roadmap" in message.lower()
