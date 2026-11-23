"""MachinaRuntime — thin facade for connector lifecycle and routing.

Exposes the runtime wiring (connectors, sandbox mode, primary CMMS)
as a standalone object for consumers like the MCP server, without
requiring the full Agent with LLM, conversation history, and channels.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import structlog

from machina.connectors.base import (
    BaseConnector,
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorStatus,
)
from machina.connectors.capabilities import Capability
from machina.exceptions import ConnectorConfigError, ConnectorError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from machina.config.schema import MachinaConfig

logger = structlog.get_logger(__name__)

# Connector type → factory import path
_CONNECTOR_FACTORIES: dict[str, str] = {
    "generic_cmms": "machina.connectors.cmms.generic.GenericCmmsConnector",
    "sap_pm": "machina.connectors.cmms.sap_pm.SapPmConnector",
    "maximo": "machina.connectors.cmms.maximo.MaximoConnector",
    "upkeep": "machina.connectors.cmms.upkeep.UpKeepConnector",
    "opcua": "machina.connectors.iot.opcua.OpcUaConnector",
    "mqtt": "machina.connectors.iot.mqtt.MqttConnector",
    "document_store": "machina.connectors.docs.document_store.DocumentStoreConnector",
    "excel": "machina.connectors.docs.excel.ExcelCsvConnector",
    "excel_csv": "machina.connectors.docs.excel.ExcelCsvConnector",
    "sql": "machina.connectors.sql.generic.GenericSqlConnector",
    "generic_sql": "machina.connectors.sql.generic.GenericSqlConnector",
    "calendar": "machina.connectors.calendar.connector.CalendarConnector",
    "telegram": "machina.connectors.comms.telegram.TelegramConnector",
    "slack": "machina.connectors.comms.slack.SlackConnector",
    "email": "machina.connectors.comms.email.EmailConnector",
}


def _import_class(dotted_path: str) -> type[Any]:
    """Import a class from a dotted module path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


_CMMS_CAPABILITIES: frozenset[Capability] = frozenset(
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

_PRIMARY_ROUTED_CAPABILITIES: frozenset[Capability] = _CMMS_CAPABILITIES - {
    Capability.READ_FAILURE_MODES
}


def _reads_assets(conn_type: str) -> bool | None:
    """Whether a connector type can read assets, judged from its class alone.

    Returns ``None`` when the type is not registered or its class cannot be
    imported: such an entry can never be built, so it can never serve a call.
    """
    factory_path = _CONNECTOR_FACTORIES.get(conn_type)
    if factory_path is None:
        return None
    try:
        connector_cls = _import_class(factory_path)
    except Exception:
        return None
    from machina.introspect.core import _class_base_capabilities

    return Capability.READ_ASSETS in _class_base_capabilities(connector_cls, conn_type)


def _resolve_primary_cmms(config: MachinaConfig) -> str | None:
    """Name the primary CMMS connector of ``config``, or ``None`` when no CMMS is enabled.

    A single enabled connector that reads assets is the primary without the
    flag. With more than one, exactly one must carry ``primary: true``. The
    config alone decides: no connector is built, so the MCP server can refuse
    a config before it serves. The messages name connectors and types, never
    a setting value.

    Raises:
        ConnectorConfigError: ``primary: true`` sits on a disabled connector,
            on more than one enabled connector, on an unknown type or on a
            type that cannot read assets; or several enabled connectors read
            assets and none is marked primary.
    """
    connectors = config.connectors
    for name, conn_cfg in connectors.items():
        if conn_cfg.primary and not conn_cfg.enabled:
            raise ConnectorConfigError(
                f"Connector {name!r} is marked primary but disabled (enabled: false). "
                "Enable it or remove 'primary: true'."
            )
    enabled = {name: cfg for name, cfg in connectors.items() if cfg.enabled}
    marked = [name for name, cfg in enabled.items() if cfg.primary]
    if len(marked) > 1:
        raise ConnectorConfigError(
            f"Multiple connectors marked primary: {marked!r} — at most one connector may be primary"
        )
    if marked:
        name = marked[0]
        conn_type = enabled[name].type
        if conn_type not in _CONNECTOR_FACTORIES:
            raise ConnectorConfigError(
                f"Connector {name!r} is marked primary, but its type {conn_type!r} "
                "is not a known connector type."
            )
        if _reads_assets(conn_type) is False:
            raise ConnectorConfigError(
                f"Connector {name!r} is marked primary, but its type {conn_type!r} "
                "cannot read assets: 'primary: true' marks the primary CMMS."
            )
        return name
    candidates = [name for name, cfg in enabled.items() if _reads_assets(cfg.type)]
    if len(candidates) > 1:
        raise ConnectorConfigError(
            f"Several enabled connectors can serve as the CMMS: {candidates!r}. "
            "Mark exactly one of them with 'primary: true'."
        )
    return candidates[0] if candidates else None


def _degraded_primary_error(name: str, error_type: str) -> ConnectorError:
    """The error every call to a primary CMMS that failed to start raises."""
    return ConnectorError(
        f"The primary CMMS connector {name!r} failed to start ({error_type}). "
        "Machina is running degraded and does not use another CMMS in its place. "
        "Fix the connector's configuration and restart Machina."
    )


class _DegradedPrimaryCmms:
    """Holds the slot of a primary CMMS that failed to start.

    It declares every CMMS capability, so each CMMS call reaches it and fails
    with the degraded error instead of moving to another connector.
    """

    def __init__(self, name: str, error_type: str) -> None:
        from machina.introspect._methods import CAPABILITY_TO_METHOD

        self.capabilities: frozenset[Capability] = _CMMS_CAPABILITIES
        self._name = name
        self._error_type = error_type
        self._methods = frozenset(CAPABILITY_TO_METHOD[cap] for cap in _CMMS_CAPABILITIES)

    def error(self) -> ConnectorError:
        """A new instance of the degraded error its calls raise."""
        return _degraded_primary_error(self._name, self._error_type)

    async def connect(self) -> None:
        """Do nothing: there is no connector to connect."""

    async def disconnect(self) -> None:
        """Do nothing: there is no connector to disconnect."""

    async def health_check(self) -> ConnectorHealth:
        """Report the primary as unhealthy."""
        return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message=str(self.error()))

    def __getattr__(self, attr: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        if attr.startswith("_") or attr not in self._methods:
            raise AttributeError(attr)

        async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise self.error()

        return _refuse


class _PrimaryCmmsRegistry(ConnectorRegistry):
    """A registry that, once scoped, sends CMMS lookups to the primary only.

    Unscoped, it behaves exactly like :class:`ConnectorRegistry`. Scoped with
    :meth:`route_cmms_to`, a lookup for a CMMS capability returns the primary
    when it declares the capability and nothing otherwise, so no CMMS call
    reaches another connector. The failure-mode catalog stays readable from
    every provider.
    """

    def __init__(self) -> None:
        super().__init__()
        self._primary_cmms: str | None = None

    def route_cmms_to(self, name: str) -> None:
        """Send every CMMS lookup to the connector registered as ``name``."""
        self._primary_cmms = name

    def find_by_capability(self, capability: Capability | str) -> list[tuple[str, BaseConnector]]:
        """Return the providers of ``capability``; see the class docstring."""
        matches = super().find_by_capability(capability)
        if self._primary_cmms is None or capability not in _PRIMARY_ROUTED_CAPABILITIES:
            return matches
        return [(name, conn) for name, conn in matches if name == self._primary_cmms]


class MachinaRuntime:
    """Runtime facade for connector lifecycle and capability routing.

    Args:
        connectors: Named connector instances.
        sandbox_mode: If True, write operations should be blocked.
        primary_cmms_name: Name of the connector marked ``primary: true``.

    Example:
        ```python
        runtime = MachinaRuntime.from_config(config)
        await runtime.connect_all()
        cmms = runtime.get_primary_cmms()
        assets = await cmms.read_assets()
        await runtime.disconnect_all()
        ```
    """

    def __init__(
        self,
        *,
        connectors: dict[str, BaseConnector] | None = None,
        sandbox_mode: bool = False,
        primary_cmms_name: str = "",
    ) -> None:
        self._registry = ConnectorRegistry()
        self.connectors: dict[str, BaseConnector] = connectors or {}
        for name, conn in self.connectors.items():
            self._registry.register(name, conn)
        self.sandbox_mode = sandbox_mode
        self._primary_cmms_name = primary_cmms_name
        self.failed_connectors: dict[str, str] = {}
        self._primary_cmms_policy_applied = False
        self._primary_cmms_failure: tuple[str, str] | None = None
        self._primary_cmms_class = ""

    @classmethod
    def from_config(cls, config: MachinaConfig) -> MachinaRuntime:
        """Build a runtime from a MachinaConfig, instantiating connectors.

        The primary CMMS follows the shared policy: the single enabled
        connector that reads assets, or the one marked ``primary: true``. A
        primary that cannot be built leaves the runtime degraded:
        :meth:`get_primary_cmms` raises instead of using another connector.
        Any other connector that cannot be built is logged and left out.

        Raises:
            ConnectorConfigError: The config marks no primary, or an invalid
                one, where the policy requires one (see the YAML config
                reference).
        """
        primary_name = _resolve_primary_cmms(config)
        primary_failure: tuple[str, str] | None = None
        connectors: dict[str, BaseConnector] = {}
        for name, conn_cfg in config.connectors.items():
            if not conn_cfg.enabled:
                continue
            factory_path = _CONNECTOR_FACTORIES.get(conn_cfg.type)
            if factory_path is None:
                logger.warning(
                    "unknown_connector_type",
                    connector_name=name,
                    connector_type=conn_cfg.type,
                )
                continue
            try:
                connector_cls = _import_class(factory_path)
                connector = connector_cls(**conn_cfg.settings)
                connectors[name] = connector
            except Exception as exc:
                logger.error(
                    "connector_instantiation_failed",
                    connector_name=name,
                    connector_type=conn_cfg.type,
                    error=str(exc),
                )
                if name == primary_name:
                    primary_failure = (name, type(exc).__name__)
                    logger.error("primary_cmms_degraded", connector=name)
        runtime = cls(
            connectors=connectors,
            sandbox_mode=config.sandbox,
            primary_cmms_name=primary_name or "",
        )
        runtime._primary_cmms_policy_applied = True
        runtime._primary_cmms_failure = primary_failure
        if primary_name is not None:
            factory_path = _CONNECTOR_FACTORIES[config.connectors[primary_name].type]
            runtime._primary_cmms_class = factory_path.rsplit(".", 1)[1]
        return runtime

    async def connect_all(self) -> None:
        """Connect all registered connectors.

        Failed connectors are recorded in ``self.failed_connectors`` and
        logged at ERROR level. The runtime continues with healthy connectors.
        """
        self.failed_connectors = {}
        for name, conn in self.connectors.items():
            try:
                await conn.connect()
                logger.info("runtime_connector_connected", connector=name)
            except Exception as exc:
                from machina.agent.tool_errors import error_diagnostics

                self.failed_connectors[name] = str(exc)
                logger.error(
                    "runtime_connector_failed",
                    connector=name,
                    error=str(exc),
                    **error_diagnostics(exc),  # the chained cause's type
                )
        if self.failed_connectors:
            logger.warning(
                "runtime_partial_startup",
                failed=list(self.failed_connectors.keys()),
                healthy=[n for n in self.connectors if n not in self.failed_connectors],
            )

    async def disconnect_all(self) -> None:
        """Disconnect all registered connectors."""
        for _name, conn in self.connectors.items():
            with contextlib.suppress(Exception):
                await conn.disconnect()

    def get_primary_cmms(self) -> BaseConnector:
        """Return the primary CMMS connector.

        For a runtime built by :meth:`from_config`, this is the connector the
        policy chose, connected or not, and never another one. A runtime
        constructed directly returns ``primary_cmms_name``'s connector, else
        the first connector that supports READ_ASSETS, with a warning.

        Raises:
            ConnectorError: If no CMMS connector is configured, or the
                primary failed to start (Machina is running degraded).
        """
        if self._primary_cmms_failure is not None:
            raise _degraded_primary_error(*self._primary_cmms_failure)
        if self._primary_cmms_name:
            conn = self._registry.get(self._primary_cmms_name)
            if conn is not None:
                return conn
        if self._primary_cmms_policy_applied:
            raise ConnectorError("No CMMS connector configured")
        matches = self._registry.find_by_capability(Capability.READ_ASSETS)
        if not matches:
            raise ConnectorError("No CMMS connector configured")
        if not self._primary_cmms_name:
            logger.warning(
                "no_primary_cmms_configured",
                using=matches[0][0],
                hint="Set 'primary: true' on one connector in config",
            )
        return matches[0][1]

    def find_by_capability(self, capability: Capability) -> list[tuple[str, BaseConnector]]:
        """Find all connectors supporting a given capability."""
        return self._registry.find_by_capability(capability)

    def _primary_cmms_class_name(self) -> str:
        """The primary CMMS connector's class name, also when it failed to start.

        ``""`` when no primary is known.
        """
        if self._primary_cmms_class:
            return self._primary_cmms_class
        conn = self._registry.get(self._primary_cmms_name) if self._primary_cmms_name else None
        return type(conn).__name__ if conn is not None else ""
