"""MachinaRuntime — thin facade for connector lifecycle and routing.

Exposes the runtime wiring (connectors, sandbox mode, primary CMMS)
as a standalone object for consumers like the MCP server, without
requiring the full Agent with LLM, conversation history, and channels.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import structlog

from machina.connectors.base import BaseConnector, ConnectorRegistry
from machina.connectors.capabilities import Capability
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
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

    @classmethod
    def from_config(cls, config: MachinaConfig) -> MachinaRuntime:
        """Build a runtime from a MachinaConfig, instantiating connectors."""
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
        primary_names = [
            name
            for name, cfg in config.connectors.items()
            if cfg.enabled and getattr(cfg, "primary", False)
        ]
        if len(primary_names) > 1:
            raise ConnectorError(
                f"Multiple connectors marked primary: {primary_names!r} — "
                "at most one connector may be primary"
            )
        primary_cmms_name = primary_names[0] if primary_names else ""
        return cls(
            connectors=connectors,
            sandbox_mode=config.sandbox,
            primary_cmms_name=primary_cmms_name,
        )

    async def connect_all(self) -> None:
        """Connect all registered connectors."""
        for name, conn in self.connectors.items():
            try:
                await conn.connect()
                logger.info("runtime_connector_connected", connector=name)
            except Exception as exc:
                logger.error(
                    "runtime_connector_failed",
                    connector=name,
                    error=str(exc),
                )

    async def disconnect_all(self) -> None:
        """Disconnect all registered connectors."""
        for _name, conn in self.connectors.items():
            with contextlib.suppress(Exception):
                await conn.disconnect()

    def get_primary_cmms(self) -> BaseConnector:
        """Return the primary CMMS connector.

        If a connector is marked ``primary: true`` in config, it is
        returned.  Otherwise falls back to the first connector that
        supports READ_ASSETS and logs a warning.

        Raises:
            ConnectorError: If no CMMS connector is configured.
        """
        if self._primary_cmms_name:
            conn = self._registry.get(self._primary_cmms_name)
            if conn is not None:
                return conn
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
