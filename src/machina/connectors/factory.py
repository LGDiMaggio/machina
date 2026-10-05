"""Connector and channel factory — instantiate from type strings.

Used by :meth:`Agent.from_config` to create connector and channel
instances from YAML configuration.

Connector types resolve through
:data:`machina.runtime._CONNECTOR_FACTORIES` — the same registry
:class:`~machina.runtime.MachinaRuntime`, the MCP server and
``machina describe`` read — so every YAML entry point accepts exactly the
same connector types.

Example::

    from machina.connectors.factory import create_connector

    conn = create_connector("generic_cmms", {"data_dir": "./data/cmms"})
"""

from __future__ import annotations

from typing import Any

from machina.exceptions import MachinaError


def _channel_registry() -> dict[str, type]:
    """Channel types for communication channels."""
    from machina.connectors.comms.cli import CliChannel
    from machina.connectors.comms.telegram import TelegramConnector

    registry: dict[str, type] = {
        "cli": CliChannel,
        "telegram": TelegramConnector,
    }

    try:
        from machina.connectors.comms.slack import SlackConnector

        registry["slack"] = SlackConnector
    except ImportError:
        pass

    try:
        from machina.connectors.comms.email import EmailConnector

        registry["email"] = EmailConnector
    except ImportError:
        pass

    return registry


def create_connector(type_name: str, settings: dict[str, Any]) -> Any:
    """Instantiate a connector by type name and settings dict.

    Args:
        type_name: Connector type (e.g. ``"generic_cmms"``, ``"opcua"``) — a
            key of :data:`machina.runtime._CONNECTOR_FACTORIES`.
        settings: Keyword arguments forwarded to the connector constructor.

    Returns:
        A connector instance.

    Raises:
        MachinaError: If the type name is not registered, or the module
            implementing it cannot be imported (usually a missing extra).
    """
    from machina.runtime import _CONNECTOR_FACTORIES, _import_class

    dotted_path = _CONNECTOR_FACTORIES.get(type_name)
    if dotted_path is None:
        available = ", ".join(sorted(_CONNECTOR_FACTORIES))
        raise MachinaError(f"Unknown connector type {type_name!r}. Available: {available}")
    try:
        cls = _import_class(dotted_path)
    except ImportError as exc:
        raise MachinaError(
            f"Connector type {type_name!r} could not be imported ({exc}). "
            "Install the extra it requires — `machina describe` lists it."
        ) from exc
    return cls(**settings)


def create_channel(type_name: str, settings: dict[str, Any]) -> Any:
    """Instantiate a communication channel by type name.

    Args:
        type_name: Channel type (e.g. ``"cli"``, ``"telegram"``).
        settings: Keyword arguments forwarded to the channel constructor.

    Returns:
        A channel instance.

    Raises:
        MachinaError: If the type name is not recognized.
    """
    registry = _channel_registry()
    cls = registry.get(type_name)
    if cls is None:
        available = ", ".join(sorted(registry.keys()))
        raise MachinaError(f"Unknown channel type {type_name!r}. Available: {available}")
    return cls(**settings)
