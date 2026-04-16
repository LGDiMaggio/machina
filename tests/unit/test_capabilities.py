"""Tests for the Capability enum and dual-accept ConnectorRegistry.

v0.3 introduces a typed `Capability` StrEnum replacing opaque `list[str]`
capabilities on connectors. The registry accepts both during deprecation;
bare strings emit ``DeprecationWarning``.
"""

from __future__ import annotations

import warnings

import pytest

from machina.connectors.base import (
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorStatus,
)
from machina.connectors.capabilities import Capability


class _FakeCmms:
    """Fake CMMS connector declaring typed capabilities."""

    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.CREATE_WORK_ORDER,
        }
    )

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def health_check(self) -> ConnectorHealth:
        return ConnectorHealth(status=ConnectorStatus.HEALTHY)


class _FakeDocs:
    """Fake document connector declaring typed capabilities."""

    capabilities: frozenset[Capability] = frozenset(
        {Capability.SEARCH_DOCUMENTS, Capability.RETRIEVE_SECTION}
    )

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def health_check(self) -> ConnectorHealth:
        return ConnectorHealth(status=ConnectorStatus.HEALTHY)


class TestCapabilityEnum:
    """Values and identity of the Capability enum."""

    def test_values_match_legacy_strings(self) -> None:
        """JSON / wire formats must not break: string value preserved verbatim."""
        assert Capability.READ_ASSETS == "read_assets"
        assert Capability.CREATE_WORK_ORDER == "create_work_order"
        assert Capability.SEARCH_DOCUMENTS == "search_documents"
        assert Capability.SEND_MESSAGE == "send_message"
        assert Capability.SUBSCRIBE_TO_TOPICS == "subscribe_to_topics"
        assert Capability.READ_CALENDAR_EVENTS == "read_calendar_events"

    def test_enum_is_string_subclass(self) -> None:
        """StrEnum: members must behave as strings for backward compat."""
        assert isinstance(Capability.READ_ASSETS, str)
        assert Capability.READ_ASSETS == "read_assets"

    def test_membership_by_value(self) -> None:
        """`Capability("read_assets")` constructs from legacy string."""
        assert Capability("read_assets") is Capability.READ_ASSETS

    def test_unknown_value_raises(self) -> None:
        """Unknown capability string raises ValueError — prevents typos."""
        with pytest.raises(ValueError):
            Capability("not_a_real_capability")


class TestRegistryDualAccept:
    """ConnectorRegistry.find_by_capability accepts Capability | str."""

    def test_enum_lookup_returns_matching_connector(self) -> None:
        registry = ConnectorRegistry()
        cmms = _FakeCmms()
        registry.register("cmms", cmms)

        results = registry.find_by_capability(Capability.READ_ASSETS)

        assert len(results) == 1
        assert results[0][0] == "cmms"
        assert results[0][1] is cmms

    def test_string_lookup_returns_same_match_with_deprecation(self) -> None:
        """Bare string still works but emits DeprecationWarning."""
        registry = ConnectorRegistry()
        cmms = _FakeCmms()
        registry.register("cmms", cmms)

        with pytest.warns(DeprecationWarning, match="Capability"):
            results = registry.find_by_capability("read_assets")

        assert len(results) == 1
        assert results[0][0] == "cmms"

    def test_enum_lookup_does_not_emit_deprecation(self) -> None:
        """Idiomatic Capability enum path must be warning-free."""
        registry = ConnectorRegistry()
        registry.register("cmms", _FakeCmms())

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            registry.find_by_capability(Capability.READ_ASSETS)

    def test_capability_declared_by_no_connector_returns_empty(self) -> None:
        """Known Capability that no registered connector declares."""
        registry = ConnectorRegistry()
        registry.register("cmms", _FakeCmms())

        results = registry.find_by_capability(Capability.PUBLISH_MESSAGE)

        assert results == []

    def test_invalid_string_returns_empty_without_raising(self) -> None:
        """Unknown capability string returns [] (silently); caller can probe."""
        registry = ConnectorRegistry()
        registry.register("cmms", _FakeCmms())

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            results = registry.find_by_capability("nonexistent_capability")

        assert results == []

    def test_find_across_multiple_connectors(self) -> None:
        registry = ConnectorRegistry()
        registry.register("cmms", _FakeCmms())
        registry.register("docs", _FakeDocs())

        cmms_only = registry.find_by_capability(Capability.CREATE_WORK_ORDER)
        docs_only = registry.find_by_capability(Capability.SEARCH_DOCUMENTS)

        assert {name for name, _ in cmms_only} == {"cmms"}
        assert {name for name, _ in docs_only} == {"docs"}


class TestConnectorsDeclareTypedCapabilities:
    """Smoke check: in-tree connectors must declare frozenset[Capability]."""

    def test_sap_pm_declares_frozenset(self) -> None:
        from machina.connectors.cmms.sap_pm import SapPmConnector

        assert isinstance(SapPmConnector.capabilities, frozenset)
        assert Capability.CREATE_WORK_ORDER in SapPmConnector.capabilities

    def test_maximo_declares_frozenset(self) -> None:
        from machina.connectors.cmms.maximo import MaximoConnector

        assert isinstance(MaximoConnector.capabilities, frozenset)
        assert Capability.READ_ASSETS in MaximoConnector.capabilities

    def test_upkeep_declares_frozenset(self) -> None:
        from machina.connectors.cmms.upkeep import UpKeepConnector

        assert isinstance(UpKeepConnector.capabilities, frozenset)
        assert Capability.UPDATE_WORK_ORDER in UpKeepConnector.capabilities
