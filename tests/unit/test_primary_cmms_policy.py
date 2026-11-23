"""The primary-CMMS policy shared by MachinaRuntime, Agent.from_config and the MCP server.

One connector that reads assets is the implicit primary; with more than one,
exactly one must carry ``primary: true``. A primary that cannot be built leaves
Machina running degraded: its calls fail and never move to another CMMS.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina import runtime as machina_runtime
from machina.config.schema import ConnectorConfig, MachinaConfig
from machina.connectors.base import ConnectorRegistry, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.exceptions import ConnectorConfigError, ConnectorError
from machina.runtime import (
    MachinaRuntime,
    _DegradedPrimaryCmms,
    _PrimaryCmmsRegistry,
    _resolve_primary_cmms,
)

_SECRET = "s3cr3t-token-value"

_CMMS_CAPABILITIES = frozenset(
    {
        Capability.READ_ASSETS,
        Capability.READ_WORK_ORDERS,
        Capability.GET_WORK_ORDER,
        Capability.CREATE_WORK_ORDER,
        Capability.UPDATE_WORK_ORDER,
        Capability.CLOSE_WORK_ORDER,
        Capability.CANCEL_WORK_ORDER,
        Capability.READ_SPARE_PARTS,
        Capability.READ_MAINTENANCE_PLANS,
        Capability.READ_MAINTENANCE_HISTORY,
        Capability.READ_FAILURE_MODES,
    }
)


class _FakeCmms:
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS, Capability.CREATE_WORK_ORDER}
    )

    def __init__(self, **settings: Any) -> None:
        self.settings = settings

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def health_check(self) -> Any:
        return None


class _BrokenCmms(_FakeCmms):
    def __init__(self, **settings: Any) -> None:
        raise ValueError(f"cannot reach {settings.get('url')}")


class _UnreachableCmms(_FakeCmms):
    async def connect(self) -> None:
        raise ConnectorError("Failed to connect (ConnectionRefusedError)")


class _InstanceOnlyCmms(_FakeCmms):
    """Reads assets, but declares it only on instances: the policy cannot see it."""

    capabilities = property(lambda self: frozenset({Capability.READ_ASSETS}))


class _FakeSensor:
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SUBSCRIBE_TO_TOPICS})

    def __init__(self, **settings: Any) -> None:
        self.settings = settings

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _Provider:
    """A registry entry with an explicit capability set."""

    def __init__(self, *caps: Capability) -> None:
        self.capabilities = frozenset(caps)


@pytest.fixture(autouse=True)
def _fake_types(monkeypatch: pytest.MonkeyPatch) -> None:
    for type_name, cls_name in {
        "fake_cmms": "_FakeCmms",
        "fake_broken": "_BrokenCmms",
        "fake_unreachable": "_UnreachableCmms",
        "fake_sensor": "_FakeSensor",
        "fake_instance_only": "_InstanceOnlyCmms",
    }.items():
        monkeypatch.setitem(
            machina_runtime._CONNECTOR_FACTORIES, type_name, f"{__name__}.{cls_name}"
        )
    monkeypatch.setitem(
        machina_runtime._CONNECTOR_FACTORIES, "fake_unimportable", "machina.no_such_module.Nothing"
    )


def _config(**connectors: ConnectorConfig) -> MachinaConfig:
    return MachinaConfig(connectors=connectors)


class TestResolvePrimaryCmms:
    def test_a_single_cmms_is_the_primary_without_the_flag(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            sensors=ConnectorConfig(type="mqtt"),
        )
        assert _resolve_primary_cmms(config) == "cmms"

    def test_two_connectors_that_read_assets_need_a_primary(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            registry=ConnectorConfig(type="excel_csv"),
        )
        with pytest.raises(ConnectorConfigError) as exc_info:
            _resolve_primary_cmms(config)
        message = str(exc_info.value)
        assert "'cmms'" in message
        assert "'registry'" in message
        assert "primary: true" in message

    def test_the_marked_connector_is_the_primary(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            registry=ConnectorConfig(type="excel_csv", primary=True),
        )
        assert _resolve_primary_cmms(config) == "registry"

    def test_a_disabled_connector_marked_primary_is_refused(self) -> None:
        config = _config(
            old=ConnectorConfig(type="generic_cmms", enabled=False, primary=True),
            new=ConnectorConfig(type="generic_cmms"),
        )
        with pytest.raises(ConnectorConfigError, match="'old'"):
            _resolve_primary_cmms(config)

    def test_a_disabled_connector_is_not_counted(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            old=ConnectorConfig(type="excel_csv", enabled=False),
        )
        assert _resolve_primary_cmms(config) == "cmms"

    def test_two_enabled_primaries_are_refused(self) -> None:
        config = _config(
            a=ConnectorConfig(type="generic_cmms", primary=True),
            b=ConnectorConfig(type="excel_csv", primary=True),
        )
        with pytest.raises(ConnectorConfigError, match="Multiple connectors marked primary"):
            _resolve_primary_cmms(config)

    def test_a_primary_of_unknown_type_is_refused(self) -> None:
        config = _config(cmms=ConnectorConfig(type="no_such_type", primary=True))
        with pytest.raises(ConnectorConfigError, match="'no_such_type'"):
            _resolve_primary_cmms(config)

    def test_a_primary_that_cannot_read_assets_is_refused(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            sensors=ConnectorConfig(type="mqtt", primary=True),
        )
        with pytest.raises(ConnectorConfigError, match=r"'sensors'.*cannot read assets"):
            _resolve_primary_cmms(config)

    def test_no_cmms_means_no_primary(self) -> None:
        assert _resolve_primary_cmms(_config(sensors=ConnectorConfig(type="mqtt"))) is None
        assert _resolve_primary_cmms(_config()) is None

    def test_an_unknown_type_is_not_counted(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            extra=ConnectorConfig(type="no_such_type"),
        )
        assert _resolve_primary_cmms(config) == "cmms"

    def test_a_type_that_cannot_be_imported_is_not_counted(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="generic_cmms"),
            extra=ConnectorConfig(type="fake_unimportable"),
        )
        assert _resolve_primary_cmms(config) == "cmms"

    def test_a_marked_primary_that_cannot_be_imported_is_kept(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="fake_unimportable", primary=True),
            registry=ConnectorConfig(type="excel_csv"),
        )
        assert _resolve_primary_cmms(config) == "cmms"


class TestRuntimeFromConfig:
    def test_a_primary_that_fails_to_build_leaves_the_runtime_degraded(self) -> None:
        config = _config(
            sap=ConnectorConfig(type="fake_broken", primary=True, settings={"url": _SECRET}),
            registry=ConnectorConfig(type="fake_cmms"),
        )
        runtime = MachinaRuntime.from_config(config)

        assert list(runtime.connectors) == ["registry"]
        with pytest.raises(ConnectorError) as exc_info:
            runtime.get_primary_cmms()
        message = str(exc_info.value)
        assert "'sap'" in message
        assert "ValueError" in message
        assert "degraded" in message
        assert "another CMMS" in message
        assert _SECRET not in message
        assert "sap" not in runtime.failed_connectors

    def test_a_marked_primary_that_cannot_be_imported_leaves_the_runtime_degraded(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="fake_unimportable", primary=True),
            registry=ConnectorConfig(type="fake_cmms"),
        )
        runtime = MachinaRuntime.from_config(config)
        with pytest.raises(ConnectorError, match=r"'cmms'.*degraded"):
            runtime.get_primary_cmms()

    def test_no_cmms_is_reported_as_unconfigured(self) -> None:
        runtime = MachinaRuntime.from_config(_config(sensors=ConnectorConfig(type="fake_sensor")))
        with pytest.raises(ConnectorError, match="No CMMS connector configured"):
            runtime.get_primary_cmms()

    def test_a_config_built_runtime_never_falls_back_to_another_connector(self) -> None:
        runtime = MachinaRuntime.from_config(
            _config(odd=ConnectorConfig(type="fake_instance_only"))
        )
        assert "odd" in runtime.connectors
        with pytest.raises(ConnectorError, match="No CMMS connector configured"):
            runtime.get_primary_cmms()

    def test_the_marked_primary_is_returned_when_it_is_not_first(self) -> None:
        config = _config(
            first=ConnectorConfig(type="fake_cmms"),
            second=ConnectorConfig(type="fake_cmms", primary=True),
        )
        runtime = MachinaRuntime.from_config(config)
        assert runtime.get_primary_cmms() is runtime.connectors["second"]

    def test_an_invalid_config_is_refused(self) -> None:
        config = _config(
            first=ConnectorConfig(type="fake_cmms"),
            second=ConnectorConfig(type="fake_cmms"),
        )
        with pytest.raises(ConnectorConfigError):
            MachinaRuntime.from_config(config)

    @pytest.mark.asyncio
    async def test_a_primary_that_fails_to_connect_stays_the_primary(self) -> None:
        config = _config(
            cmms=ConnectorConfig(type="fake_unreachable", primary=True),
            registry=ConnectorConfig(type="fake_cmms"),
        )
        runtime = MachinaRuntime.from_config(config)
        await runtime.connect_all()

        assert "cmms" in runtime.failed_connectors
        assert runtime.get_primary_cmms() is runtime.connectors["cmms"]

    def test_the_primary_class_is_known_even_when_it_failed_to_build(self) -> None:
        config = _config(
            sap=ConnectorConfig(type="fake_broken", primary=True),
            registry=ConnectorConfig(type="fake_cmms"),
        )
        assert MachinaRuntime.from_config(config)._primary_cmms_class_name() == "_BrokenCmms"

    def test_a_directly_built_runtime_names_its_primary_class(self) -> None:
        named = MachinaRuntime(connectors={"cmms": _FakeCmms()}, primary_cmms_name="cmms")
        assert named._primary_cmms_class_name() == "_FakeCmms"
        assert MachinaRuntime(connectors={"cmms": _FakeCmms()})._primary_cmms_class_name() == ""


class TestPrimaryCmmsRegistry:
    def _registry(self) -> tuple[_PrimaryCmmsRegistry, _Provider, _Provider, _Provider]:
        primary = _Provider(Capability.READ_ASSETS, Capability.READ_WORK_ORDERS)
        secondary = _Provider(
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.CREATE_WORK_ORDER,
            Capability.READ_FAILURE_MODES,
        )
        docs = _Provider(Capability.SEARCH_DOCUMENTS, Capability.READ_FAILURE_MODES)
        registry = _PrimaryCmmsRegistry()
        registry.register("secondary", secondary)  # type: ignore[arg-type]
        registry.register("primary", primary)  # type: ignore[arg-type]
        registry.register("docs", docs)  # type: ignore[arg-type]
        return registry, primary, secondary, docs

    def test_an_unscoped_registry_returns_every_provider(self) -> None:
        registry, primary, secondary, _docs = self._registry()
        base = ConnectorRegistry()
        for name, conn in registry.all().items():
            base.register(name, conn)
        for cap in Capability:
            assert registry.find_by_capability(cap) == base.find_by_capability(cap)
        assert registry.find_by_capability(Capability.READ_ASSETS) == [
            ("secondary", secondary),
            ("primary", primary),
        ]

    def test_a_scoped_registry_sends_cmms_lookups_to_the_primary_only(self) -> None:
        registry, primary, secondary, docs = self._registry()
        registry.route_cmms_to("primary")

        assert registry.find_by_capability(Capability.READ_ASSETS) == [("primary", primary)]
        assert registry.find_by_capability(Capability.READ_WORK_ORDERS) == [("primary", primary)]
        assert registry.find_by_capability(Capability.CREATE_WORK_ORDER) == []
        assert registry.find_by_capability(Capability.READ_FAILURE_MODES) == [
            ("secondary", secondary),
            ("docs", docs),
        ]
        assert registry.find_by_capability(Capability.SEARCH_DOCUMENTS) == [("docs", docs)]


class TestDegradedPrimaryPlaceholder:
    def _placeholder(self) -> _DegradedPrimaryCmms:
        return _DegradedPrimaryCmms("sap", "ValueError")

    def test_it_declares_every_cmms_capability(self) -> None:
        assert self._placeholder().capabilities == _CMMS_CAPABILITIES

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method",
        ["read_assets", "read_work_orders", "create_work_order", "read_spare_parts"],
    )
    async def test_its_cmms_methods_raise_the_degraded_error(self, method: str) -> None:
        with pytest.raises(ConnectorError, match=r"'sap'.*degraded"):
            await getattr(self._placeholder(), method)(asset_id="P-1")

    @pytest.mark.asyncio
    async def test_its_lifecycle_does_nothing_and_reports_unhealthy(self) -> None:
        placeholder = self._placeholder()
        await placeholder.connect()
        await placeholder.disconnect()
        health = await placeholder.health_check()
        assert health.status == ConnectorStatus.UNHEALTHY
        assert "'sap'" in health.message

    def test_other_attributes_do_not_exist(self) -> None:
        placeholder = self._placeholder()
        assert not hasattr(placeholder, "_maintenance_plans")
        with pytest.raises(AttributeError):
            _ = placeholder.url
