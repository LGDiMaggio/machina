"""Integration tests for the ``machina://v1/capabilities`` MCP resource.

The resource exposes the code-derived self-description (the introspection
``Spine``) over MCP, mirroring the static ``read_failure_taxonomy`` resource:
runtime-free, served from memory, and usable with zero connectors configured.

Trust model: ACCEPT DISCLOSURE — ``describe()`` carries no configured values
(types/shape only), so the resource needs no separate auth scope and nothing
extra to scrub.
"""

from __future__ import annotations

import json
import sys

import pytest

# Gate the whole module on the MCP SDK, mirroring tests/unit/mcp/conftest.py.
pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")

from machina.config.schema import MachinaConfig
from machina.introspect import describe
from machina.introspect.render_llms import render_json

CAPABILITIES_URI = "machina://v1/capabilities"

# Heavy optional transport deps that the introspection read MUST NOT import.
_HEAVY_OPTIONAL_DEPS = ("asyncua", "chromadb")


def _capability_values(payload: dict[str, object]) -> set[str]:
    """Extract the set of capability values from a capabilities payload."""
    caps = payload["capabilities"]
    assert isinstance(caps, list)
    return {c["value"] for c in caps}


class TestCapabilitiesResourceRegistration:
    def test_resource_is_static(self) -> None:
        """The capabilities resource is a static (non-template) resource."""
        from machina.mcp.server import build_server

        server = build_server(MachinaConfig())
        resources = server._resource_manager.list_resources()
        uris = [str(r.uri) for r in resources]
        assert CAPABILITIES_URI in uris

    def test_resource_name(self) -> None:
        from machina.mcp.server import build_server

        server = build_server(MachinaConfig())
        resources = server._resource_manager.list_resources()
        names = {r.name for r in resources}
        assert "machina_capabilities" in names


class TestCapabilitiesResourceRead:
    @pytest.mark.asyncio
    async def test_capability_set_matches_describe(self) -> None:
        """(a) Reading the resource returns describe()'s capability set."""
        from machina.mcp.server import build_server

        server = build_server(MachinaConfig())
        results = await server.read_resource(CAPABILITIES_URI)
        content = list(results)
        assert len(content) == 1

        payload = json.loads(content[0].content)
        expected = render_json(describe())

        # Whole payload mirrors the docs/capabilities.json shape exactly.
        assert payload == expected
        # And the capability vocabulary specifically matches describe().
        assert _capability_values(payload) == _capability_values(expected)
        spine_caps = {c.value for c in describe().capabilities}
        assert _capability_values(payload) == spine_caps

    @pytest.mark.asyncio
    async def test_works_with_no_connectors_configured(self) -> None:
        """(b) The handler returns the framework catalog with zero connectors."""
        config = MachinaConfig()
        assert not config.connectors  # nothing configured

        from machina.mcp.server import build_server

        server = build_server(config)
        results = await server.read_resource(CAPABILITIES_URI)
        content = list(results)
        assert len(content) == 1

        payload = json.loads(content[0].content)
        # Framework-level catalog is present regardless of configured connectors:
        # the connector *types* and capability vocabulary are code-derived.
        assert payload["connectors"], "expected framework connector catalog"
        assert payload["capabilities"], "expected framework capability vocabulary"
        assert _capability_values(payload) == {c.value for c in describe().capabilities}

    @pytest.mark.asyncio
    async def test_carries_no_configured_values(self) -> None:
        """ACCEPT DISCLOSURE: payload is shape-only (config_schema, no values)."""
        from machina.mcp.server import build_server

        server = build_server(MachinaConfig())
        results = await server.read_resource(CAPABILITIES_URI)
        payload = json.loads(next(iter(results)).content)

        # config_schema is a JSON-schema shape (has "properties"/"$defs"),
        # never a populated config instance.
        schema = payload["config_schema"]
        assert isinstance(schema, dict)
        assert "properties" in schema


class TestCapabilitiesResourceNoHeavyDeps:
    @pytest.mark.asyncio
    async def test_read_imports_no_heavy_optional_dep(self) -> None:
        """(c) Reading the resource imports no heavy optional dep (asyncua/chromadb)."""
        # Skip cleanly if the env happens to have a heavy dep already imported
        # by another test — we can only assert about deps not yet present.
        already_present = [m for m in _HEAVY_OPTIONAL_DEPS if m in sys.modules]
        if already_present:
            pytest.skip(f"heavy dep(s) already imported: {already_present}")

        from machina.mcp.server import build_server

        server = build_server(MachinaConfig())
        await server.read_resource(CAPABILITIES_URI)

        for mod in _HEAVY_OPTIONAL_DEPS:
            assert mod not in sys.modules, f"reading capabilities imported {mod!r}"
