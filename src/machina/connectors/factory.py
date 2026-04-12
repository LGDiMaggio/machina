"""Connector and channel factory — instantiate from type strings.

Used by :meth:`Agent.from_config` to create connector and channel
instances from YAML configuration.

Example::

    from machina.connectors.factory import create_connector

    conn = create_connector("generic_cmms", {"data_dir": "./data/cmms"})
"""

from __future__ import annotations

from typing import Any

from machina.exceptions import MachinaError


def _connector_registry() -> dict[str, type]:
    """Lazy import to avoid circular dependencies and heavy optional deps."""
    from machina.connectors.cmms import (
        GenericCmmsConnector,
        MaximoConnector,
        SapPmConnector,
        UpKeepConnector,
    )
    from machina.connectors.comms.telegram import CliChannel, TelegramConnector
    from machina.connectors.docs import DocumentStoreConnector
    from machina.connectors.iot import MqttConnector, OpcUaConnector, SimulatedSensorConnector

    # Lazy imports for optional connectors
    registry: dict[str, type] = {
        # CMMS
        "generic_cmms": GenericCmmsConnector,
        "sap_pm": SapPmConnector,
        "maximo": MaximoConnector,
        "upkeep": UpKeepConnector,
        # IoT
        "opcua": OpcUaConnector,
        "mqtt": MqttConnector,
        "simulated_sensor": SimulatedSensorConnector,
        # Documents
        "document_store": DocumentStoreConnector,
        # Communication
        "telegram": TelegramConnector,
        "cli": CliChannel,
    }

    # Optional connectors (may not be installed)
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

    try:
        from machina.connectors.calendar import CalendarConnector

        registry["calendar"] = CalendarConnector
    except ImportError:
        pass

    return registry


def _channel_registry() -> dict[str, type]:
    """Channel types for communication channels."""
    from machina.connectors.comms.telegram import CliChannel, TelegramConnector

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
        type_name: Connector type (e.g. ``"generic_cmms"``, ``"opcua"``).
        settings: Keyword arguments forwarded to the connector constructor.

    Returns:
        A connector instance.

    Raises:
        MachinaError: If the type name is not recognized.
    """
    registry = _connector_registry()
    cls = registry.get(type_name)
    if cls is None:
        available = ", ".join(sorted(registry.keys()))
        raise MachinaError(f"Unknown connector type {type_name!r}. Available: {available}")
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
