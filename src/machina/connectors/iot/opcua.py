"""OpcUaConnector — OPC-UA client for real-time industrial sensor data.

Provides subscription-based and on-demand reading of OPC-UA nodes from
PLCs, SCADA systems, and other OPC-UA servers.  Normalises value changes
into :class:`~machina.domain.alarm.Alarm` domain entities when a
configured threshold is exceeded.

Uses ``asyncua`` under the hood (install with ``pip install machina-ai[opcua]``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.domain.alarm import Alarm, Severity
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)

# Type alias for the alarm callback
AlarmCallback = Callable[[Alarm], Coroutine[Any, Any, None]]


@dataclass
class SubscriptionConfig:
    """Configuration for a single OPC-UA node subscription.

    Args:
        node_id: OPC-UA node identifier (e.g. ``"ns=2;s=Pump.P201.Vibration"``).
        sampling_interval_ms: How often the server samples the value (milliseconds).
        asset_id: Machina asset ID to associate alarms with.
        parameter: Measured parameter name (e.g. ``"vibration_velocity"``).
        threshold: Value above which an alarm is raised.
        unit: Engineering unit (e.g. ``"mm/s"``, ``"°C"``).
        severity: Alarm severity when threshold is exceeded.
    """

    node_id: str
    sampling_interval_ms: int = 1000
    asset_id: str = ""
    parameter: str = ""
    threshold: float = 0.0
    unit: str = ""
    severity: Severity = Severity.WARNING


@dataclass
class Subscription:
    """Handle for an active OPC-UA subscription.

    Returned by :meth:`OpcUaConnector.subscribe` to allow later
    cancellation via :meth:`OpcUaConnector.unsubscribe`.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _opcua_subscription: Any = field(default=None, repr=False)
    _handles: list[Any] = field(default_factory=list, repr=False)


class OpcUaConnector:
    """Connector for OPC-UA servers.

    Reads real-time sensor data from OPC-UA-enabled PLCs and SCADA
    systems and maps value changes to Machina :class:`~machina.domain.alarm.Alarm`
    entities.

    Args:
        endpoint: OPC-UA server URL (e.g. ``"opc.tcp://plc-line2:4840"``).
        security_mode: OPC-UA security mode (``"None"``, ``"Sign"``, ``"SignAndEncrypt"``).
        security_policy: Security policy URI (e.g. ``"Basic256Sha256"``).
        certificate: Path to client X.509 certificate file.
        private_key: Path to client private key file.
        username: Username for user-token authentication.
        password: Password for user-token authentication.
        subscriptions: Pre-configured node subscriptions.
        session_timeout: Session timeout in milliseconds.

    Example:
        ```python
        from machina.connectors.iot import OpcUaConnector

        opcua = OpcUaConnector(
            endpoint="opc.tcp://plc-line2:4840",
            security_mode="SignAndEncrypt",
            subscriptions=[
                {"node_id": "ns=2;s=Pump.P201.Vibration.DE",
                 "sampling_interval_ms": 1000,
                 "asset_id": "P-201",
                 "parameter": "vibration_velocity",
                 "threshold": 6.0, "unit": "mm/s"},
            ],
        )
        await opcua.connect()

        async def on_alarm(alarm):
            print(f"Alarm: {alarm.parameter}={alarm.value} {alarm.unit}")

        sub = await opcua.subscribe(on_alarm)
        ```
    """

    capabilities: ClassVar[list[str]] = [
        "subscribe_to_nodes",
        "read_node_value",
        "read_node_values",
        "browse_nodes",
    ]

    def __init__(
        self,
        *,
        endpoint: str = "",
        security_mode: str = "None",
        security_policy: str = "",
        certificate: str = "",
        private_key: str = "",
        username: str = "",
        password: str = "",
        subscriptions: list[SubscriptionConfig | dict[str, Any]] | None = None,
        session_timeout: int = 30_000,
    ) -> None:
        self._endpoint = endpoint
        self._security_mode = security_mode
        self._security_policy = security_policy
        self._certificate = certificate
        self._private_key = private_key
        self._username = username
        self._password = password
        self._session_timeout = session_timeout
        self._connected = False
        self._client: Any = None
        self._subscriptions: dict[str, Subscription] = {}

        # Normalise subscription configs
        self._sub_configs: list[SubscriptionConfig] = []
        for s in subscriptions or []:
            if isinstance(s, dict):
                self._sub_configs.append(SubscriptionConfig(**s))
            else:
                self._sub_configs.append(s)

    async def connect(self) -> None:
        """Connect to the OPC-UA server.

        Raises:
            ConnectorError: If ``endpoint`` is empty or connection fails.
            ConnectorAuthError: If authentication/security negotiation fails.
            ImportError: If ``asyncua`` is not installed.
        """
        if not self._endpoint:
            raise ConnectorError("endpoint is required for OpcUaConnector")

        try:
            from asyncua import Client
        except ImportError:
            msg = (
                "asyncua is required for OpcUaConnector. "
                "Install with: pip install machina-ai[opcua]"
            )
            raise ImportError(msg) from None

        self._client = Client(url=self._endpoint, timeout=self._session_timeout / 1000)

        # Security configuration
        try:
            await self._configure_security()
        except Exception as exc:
            raise ConnectorAuthError(f"Security configuration failed: {exc}") from exc

        # User-token authentication
        if self._username:
            self._client.set_user(self._username)
            self._client.set_password(self._password)

        try:
            await self._client.connect()
        except Exception as exc:
            err_msg = str(exc).lower()
            if "security" in err_msg or "auth" in err_msg or "certificate" in err_msg:
                raise ConnectorAuthError(f"OPC-UA authentication failed: {exc}") from exc
            raise ConnectorError(
                f"Failed to connect to OPC-UA server at {self._endpoint}: {exc}"
            ) from exc

        self._connected = True
        logger.info(
            "connected",
            connector="OpcUaConnector",
            endpoint=self._endpoint,
            security_mode=self._security_mode,
        )

        if self._security_mode == "None":
            logger.warning(
                "insecure_connection",
                connector="OpcUaConnector",
                endpoint=self._endpoint,
                message="OPC-UA security_mode is 'None'. Use 'SignAndEncrypt' in production.",
            )

    async def disconnect(self) -> None:
        """Disconnect from the OPC-UA server and cancel all subscriptions."""
        for sub in list(self._subscriptions.values()):
            await self._cancel_subscription(sub)
        self._subscriptions.clear()

        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.debug("disconnect_error", connector="OpcUaConnector", exc_info=True)
        self._client = None
        self._connected = False
        logger.info("disconnected", connector="OpcUaConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check OPC-UA server connectivity.

        Reads the server status node to verify the connection is alive.
        """
        if not self._connected or self._client is None:
            return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message="Not connected")
        try:
            from asyncua import ua

            node = self._client.get_node(ua.ObjectIds.Server_ServerStatus_State)
            await node.read_value()
            return ConnectorHealth(
                status=ConnectorStatus.HEALTHY,
                message="Connected",
                details={"endpoint": self._endpoint},
            )
        except Exception as exc:
            return ConnectorHealth(
                status=ConnectorStatus.DEGRADED,
                message=f"Server reachable but status check failed: {exc}",
                details={"endpoint": self._endpoint},
            )

    async def subscribe(
        self,
        callback: AlarmCallback,
        *,
        configs: list[SubscriptionConfig | dict[str, Any]] | None = None,
    ) -> Subscription:
        """Subscribe to OPC-UA node value changes.

        Creates a subscription for each configured node. When a value change
        exceeds the configured threshold, an :class:`Alarm` is created and
        passed to the callback.

        Args:
            callback: Async function called with each :class:`Alarm`.
            configs: Optional subscription configs; defaults to the
                     configs passed at construction time.

        Returns:
            A :class:`Subscription` handle for later cancellation.

        Raises:
            ConnectorError: If not connected.
        """
        self._ensure_connected()

        sub_configs = self._normalise_configs(configs) if configs else self._sub_configs
        if not sub_configs:
            raise ConnectorError("No subscription configurations provided")

        handler = _DataChangeHandler(
            sub_configs=sub_configs,
            callback=callback,
            endpoint=self._endpoint,
        )

        period = min(c.sampling_interval_ms for c in sub_configs)
        opcua_sub = await self._client.create_subscription(period, handler)

        handles = []
        for cfg in sub_configs:
            node = self._client.get_node(cfg.node_id)
            handle = await opcua_sub.subscribe_data_change(node)
            handles.append(handle)
            logger.debug(
                "node_subscribed",
                connector="OpcUaConnector",
                node_id=cfg.node_id,
                sampling_interval_ms=cfg.sampling_interval_ms,
                asset_id=cfg.asset_id,
            )

        sub = Subscription(_opcua_subscription=opcua_sub, _handles=handles)
        self._subscriptions[sub.id] = sub

        logger.info(
            "subscribed",
            connector="OpcUaConnector",
            subscription_id=sub.id,
            node_count=len(sub_configs),
        )
        return sub

    async def unsubscribe(self, subscription: Subscription) -> None:
        """Cancel an active subscription.

        Args:
            subscription: The subscription handle returned by :meth:`subscribe`.
        """
        await self._cancel_subscription(subscription)
        self._subscriptions.pop(subscription.id, None)
        logger.info(
            "unsubscribed",
            connector="OpcUaConnector",
            subscription_id=subscription.id,
        )

    async def read_value(self, node_id: str) -> Any:
        """Read the current value of a single OPC-UA node.

        Args:
            node_id: OPC-UA node identifier.

        Returns:
            The current node value.

        Raises:
            ConnectorError: If not connected or read fails.
        """
        self._ensure_connected()
        try:
            node = self._client.get_node(node_id)
            return await node.read_value()
        except Exception as exc:
            raise ConnectorError(f"Failed to read node {node_id}: {exc}") from exc

    async def read_values(self, node_ids: list[str]) -> dict[str, Any]:
        """Read current values of multiple OPC-UA nodes.

        Args:
            node_ids: List of OPC-UA node identifiers.

        Returns:
            Mapping of node ID to its current value.

        Raises:
            ConnectorError: If not connected or any read fails.
        """
        self._ensure_connected()
        results: dict[str, Any] = {}
        for node_id in node_ids:
            results[node_id] = await self.read_value(node_id)
        return results

    async def browse_nodes(self, root_node_id: str = "") -> list[dict[str, str]]:
        """Browse child nodes of the given root.

        Args:
            root_node_id: Starting node; defaults to the server's Objects folder.

        Returns:
            List of dicts with ``node_id``, ``browse_name``, and ``node_class``.

        Raises:
            ConnectorError: If not connected or browse fails.
        """
        self._ensure_connected()
        try:
            from asyncua import ua

            if root_node_id:
                root = self._client.get_node(root_node_id)
            else:
                root = self._client.get_node(ua.ObjectIds.ObjectsFolder)

            children = await root.get_children()
            nodes: list[dict[str, str]] = []
            for child in children:
                browse_name = await child.read_browse_name()
                node_class = await child.read_node_class()
                nodes.append(
                    {
                        "node_id": child.nodeid.to_string(),
                        "browse_name": browse_name.to_string(),
                        "node_class": node_class.name,
                    }
                )
            return nodes
        except Exception as exc:
            raise ConnectorError(f"Failed to browse nodes: {exc}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise ConnectorError("Not connected — call connect() first")

    async def _configure_security(self) -> None:
        """Apply security settings to the OPC-UA client."""
        if self._security_mode == "None":
            return

        policy_map: dict[str, str] = {
            "Basic256Sha256": "Basic256Sha256",
            "Basic128Rsa15": "Basic128Rsa15",
            "Basic256": "Basic256",
        }
        policy_name = policy_map.get(self._security_policy, self._security_policy)

        mode_map: dict[str, str] = {
            "Sign": "Sign",
            "SignAndEncrypt": "SignAndEncrypt",
        }
        mode_name = mode_map.get(self._security_mode, self._security_mode)

        await self._client.set_security_string(
            f"{policy_name},{mode_name},{self._certificate},{self._private_key}"
        )

    async def _cancel_subscription(self, sub: Subscription) -> None:
        """Cancel an OPC-UA subscription and all its monitored items."""
        if sub._opcua_subscription is not None:
            try:
                await sub._opcua_subscription.delete()
            except Exception:
                logger.debug(
                    "subscription_delete_error",
                    connector="OpcUaConnector",
                    subscription_id=sub.id,
                    exc_info=True,
                )

    @staticmethod
    def _normalise_configs(
        configs: list[SubscriptionConfig | dict[str, Any]],
    ) -> list[SubscriptionConfig]:
        """Convert mixed config dicts/dataclasses to SubscriptionConfig list."""
        result: list[SubscriptionConfig] = []
        for c in configs:
            if isinstance(c, dict):
                result.append(SubscriptionConfig(**c))
            else:
                result.append(c)
        return result


class _DataChangeHandler:
    """OPC-UA subscription handler that converts value changes to Alarms.

    Implements the ``asyncua`` subscription handler interface
    (``datachange_notification``).
    """

    def __init__(
        self,
        *,
        sub_configs: list[SubscriptionConfig],
        callback: AlarmCallback,
        endpoint: str,
    ) -> None:
        self._configs_by_node: dict[str, SubscriptionConfig] = {
            cfg.node_id: cfg for cfg in sub_configs
        }
        self._callback = callback
        self._endpoint = endpoint
        self._background_tasks: set[asyncio.Task[None]] = set()

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        """Called by asyncua on every subscribed data change.

        Schedules the async alarm processing on the running event loop.
        Each task is tracked in ``_background_tasks`` to prevent
        garbage-collection of in-flight tasks under rapid data changes.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "no_event_loop",
                connector="OpcUaConnector",
                node_id=str(node),
            )
            return

        task = loop.create_task(self._process_change(node, val))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_change(self, node: Any, val: Any) -> None:
        """Process a single data change and invoke callback if threshold exceeded."""
        node_id = node.nodeid.to_string()
        cfg = self._configs_by_node.get(node_id)

        if cfg is None:
            logger.debug(
                "unknown_node_change",
                connector="OpcUaConnector",
                node_id=node_id,
            )
            return

        try:
            value = float(val)
        except (TypeError, ValueError):
            logger.debug(
                "non_numeric_value",
                connector="OpcUaConnector",
                node_id=node_id,
                value=val,
            )
            return

        # Only raise an alarm when threshold is configured and exceeded
        if cfg.threshold and value <= cfg.threshold:
            return

        alarm = Alarm(
            id=f"ALM-{uuid.uuid4().hex[:8]}",
            asset_id=cfg.asset_id or node_id,
            severity=cfg.severity,
            parameter=cfg.parameter or node_id,
            value=value,
            threshold=cfg.threshold,
            unit=cfg.unit,
            timestamp=datetime.now(UTC),
            source=f"opcua://{self._endpoint}/{node_id}",
        )

        logger.info(
            "alarm_raised",
            connector="OpcUaConnector",
            alarm_id=alarm.id,
            asset_id=alarm.asset_id,
            parameter=alarm.parameter,
            value=alarm.value,
            threshold=alarm.threshold,
        )

        try:
            await self._callback(alarm)
        except Exception:
            logger.error(
                "alarm_callback_error",
                connector="OpcUaConnector",
                alarm_id=alarm.id,
                exc_info=True,
            )
