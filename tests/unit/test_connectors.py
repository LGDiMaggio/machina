"""Tests for the ConnectorRegistry."""

from machina.connectors.base import (
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorStatus,
)


class _FakeCmms:
    """Fake CMMS connector for testing."""

    @property
    def capabilities(self) -> list[str]:
        return ["read_assets", "read_work_orders", "create_work_order"]

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def health_check(self) -> ConnectorHealth:
        return ConnectorHealth(status=ConnectorStatus.HEALTHY)


class _FakeDocs:
    """Fake document connector for testing."""

    @property
    def capabilities(self) -> list[str]:
        return ["search_documents", "retrieve_section"]

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def health_check(self) -> ConnectorHealth:
        return ConnectorHealth(status=ConnectorStatus.HEALTHY)


class TestConnectorRegistry:
    """Test ConnectorRegistry register/lookup."""

    def test_register_and_get(self) -> None:
        registry = ConnectorRegistry()
        cmms = _FakeCmms()
        registry.register("cmms", cmms)
        assert registry.get("cmms") is cmms

    def test_get_missing_returns_none(self) -> None:
        registry = ConnectorRegistry()
        assert registry.get("nonexistent") is None

    def test_find_by_capability(self) -> None:
        registry = ConnectorRegistry()
        cmms = _FakeCmms()
        docs = _FakeDocs()
        registry.register("cmms", cmms)
        registry.register("docs", docs)

        results = registry.find_by_capability("read_assets")
        assert len(results) == 1
        assert results[0][0] == "cmms"

    def test_find_by_shared_capability(self) -> None:
        registry = ConnectorRegistry()
        cmms = _FakeCmms()
        docs = _FakeDocs()
        registry.register("cmms", cmms)
        registry.register("docs", docs)

        # Neither shares this capability
        results = registry.find_by_capability("send_message")
        assert len(results) == 0

    def test_all_connectors(self) -> None:
        registry = ConnectorRegistry()
        cmms = _FakeCmms()
        docs = _FakeDocs()
        registry.register("cmms", cmms)
        registry.register("docs", docs)
        assert len(registry.all()) == 2
