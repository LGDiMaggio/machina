"""MqttConnector — MQTT client for IoT sensor data ingestion.

Subscribes to MQTT topics, parses incoming messages (JSON, Sparkplug B,
or raw payloads), and normalises them into
:class:`~machina.domain.alarm.Alarm` domain entities when configured
thresholds are exceeded.

Uses ``aiomqtt`` under the hood (install with ``pip install machina-ai[mqtt]``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.domain.alarm import Alarm, Severity
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)

# Type alias for the alarm callback
AlarmCallback = Callable[[Alarm], Coroutine[Any, Any, None]]


class PayloadFormat(StrEnum):
    """Supported MQTT payload formats."""

    JSON = "json"
    SPARKPLUG_B = "sparkplug_b"
    RAW = "raw"


@dataclass
class TopicConfig:
    """Configuration for a single MQTT topic subscription.

    Args:
        topic: MQTT topic filter (e.g. ``"plant/sensors/pump-201/vibration"``).
        qos: MQTT Quality of Service level (0, 1, or 2).
        asset_id: Machina asset ID to associate alarms with.
        parameter: Measured parameter name (e.g. ``"vibration_velocity"``).
        threshold: Value above which an alarm is raised.
        unit: Engineering unit (e.g. ``"mm/s"``, ``"°C"``).
        severity: Alarm severity when threshold is exceeded.
        payload_format: How to decode the MQTT payload.
        value_path: JSONPath-like key for extracting values from JSON payloads
                    (e.g. ``"data.value"`` for ``{"data": {"value": 12.5}}``).
    """

    topic: str
    qos: int = 0
    asset_id: str = ""
    parameter: str = ""
    threshold: float = 0.0
    unit: str = ""
    severity: Severity = Severity.WARNING
    payload_format: PayloadFormat | str = PayloadFormat.JSON
    value_path: str = "value"


@dataclass
class MqttSubscription:
    """Handle for an active MQTT subscription session.

    Returned by :meth:`MqttConnector.subscribe` to allow later
    cancellation via :meth:`MqttConnector.unsubscribe`.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class _SubscriptionEntry:
    """Internal record linking a callback to its topic configs."""

    callback: AlarmCallback
    topic_cfgs: list[TopicConfig]


class MqttConnector:
    """Connector for MQTT brokers.

    Ingests IoT sensor data from MQTT topics and maps messages to
    Machina :class:`~machina.domain.alarm.Alarm` entities.  Supports
    JSON payloads, Sparkplug B format, and raw numeric values.

    Args:
        broker: MQTT broker hostname or IP.
        port: Broker port (default 1883, use 8883 for TLS).
        username: Username for broker authentication.
        password: Password for broker authentication.
        client_id: MQTT client identifier; auto-generated if empty.
        tls: Whether to use TLS for the connection.
        ca_certs: Path to CA certificate file for TLS verification.
        topics: Pre-configured topic subscriptions.
        keepalive: Keep-alive interval in seconds.

    Example:
        ```python
        from machina.connectors.iot import MqttConnector

        mqtt = MqttConnector(
            broker="mqtt.example.com",
            port=1883,
            topics=[
                {"topic": "plant/sensors/pump-201/vibration",
                 "asset_id": "P-201",
                 "parameter": "vibration_velocity",
                 "threshold": 6.0, "unit": "mm/s",
                 "payload_format": "json", "value_path": "data.value"},
            ],
        )
        await mqtt.connect()

        async def on_alarm(alarm):
            print(f"Alarm: {alarm.parameter}={alarm.value} {alarm.unit}")

        sub = await mqtt.subscribe(on_alarm)
        ```
    """

    capabilities: ClassVar[list[str]] = [
        "subscribe_to_topics",
        "publish_message",
    ]

    def __init__(
        self,
        *,
        broker: str = "",
        port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: str = "",
        tls: bool = False,
        ca_certs: str = "",
        topics: list[TopicConfig | dict[str, Any]] | None = None,
        keepalive: int = 60,
    ) -> None:
        self._broker = broker
        self._port = port
        self._username = username
        self._password = password
        self._client_id = client_id or f"machina-{uuid.uuid4().hex[:8]}"
        self._tls = tls
        self._ca_certs = ca_certs
        self._keepalive = keepalive
        self._connected = False
        self._client: Any = None
        self._client_cm: Any = None  # async context manager
        self._subscriptions: dict[str, MqttSubscription] = {}
        self._sub_entries: dict[str, _SubscriptionEntry] = {}
        self._reader_task: asyncio.Task[None] | None = None

        # Normalise topic configs
        self._topic_configs: list[TopicConfig] = []
        for t in topics or []:
            if isinstance(t, dict):
                self._topic_configs.append(TopicConfig(**t))
            else:
                self._topic_configs.append(t)

    async def connect(self) -> None:
        """Connect to the MQTT broker.

        Raises:
            ConnectorError: If ``broker`` is empty or connection fails.
            ConnectorAuthError: If authentication fails.
            ImportError: If ``aiomqtt`` is not installed.
        """
        if not self._broker:
            raise ConnectorError("broker is required for MqttConnector")

        try:
            import aiomqtt
        except ImportError:
            msg = (
                "aiomqtt is required for MqttConnector. Install with: pip install machina-ai[mqtt]"
            )
            raise ImportError(msg) from None

        try:
            tls_params: Any = None
            if self._tls:
                import ssl

                tls_context = ssl.create_default_context()
                if self._ca_certs:
                    tls_context.load_verify_locations(self._ca_certs)
                tls_params = tls_context

            self._client_cm = aiomqtt.Client(
                hostname=self._broker,
                port=self._port,
                username=self._username or None,
                password=self._password or None,
                identifier=self._client_id,
                tls_context=tls_params,
                keepalive=self._keepalive,
            )
            self._client = await self._client_cm.__aenter__()

        except Exception as exc:
            err_msg = str(exc).lower()
            if "auth" in err_msg or "not authorized" in err_msg or "bad user" in err_msg:
                raise ConnectorAuthError(f"MQTT authentication failed: {exc}") from exc
            raise ConnectorError(
                f"Failed to connect to MQTT broker at {self._broker}:{self._port}: {exc}"
            ) from exc

        self._connected = True
        logger.info(
            "connected",
            connector="MqttConnector",
            broker=self._broker,
            port=self._port,
            tls=self._tls,
        )

        if not self._tls:
            logger.warning(
                "insecure_connection",
                connector="MqttConnector",
                broker=self._broker,
                port=self._port,
                message="MQTT TLS is disabled. "
                "Use tls=True and port 8883 in production.",
            )

    async def disconnect(self) -> None:
        """Disconnect from the MQTT broker and cancel all subscriptions."""
        self._sub_entries.clear()
        self._subscriptions.clear()

        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._client_cm is not None:
            try:
                await self._client_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("disconnect_error", connector="MqttConnector", exc_info=True)
        self._client = None
        self._client_cm = None
        self._connected = False
        logger.info("disconnected", connector="MqttConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check MQTT broker connectivity."""
        if not self._connected or self._client is None:
            return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message="Not connected")
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message="Connected",
            details={"broker": self._broker, "port": self._port},
        )

    async def subscribe(
        self,
        callback: AlarmCallback,
        *,
        configs: list[TopicConfig | dict[str, Any]] | None = None,
    ) -> MqttSubscription:
        """Subscribe to MQTT topics and process incoming messages.

        For each message that exceeds a configured threshold an
        :class:`Alarm` is passed to *callback*.

        Multiple calls to ``subscribe()`` are supported — a single
        background reader fans out each message to all registered
        subscriptions.

        Args:
            callback: Async function called with each :class:`Alarm`.
            configs: Optional topic configs; defaults to the configs
                     passed at construction time.

        Returns:
            An :class:`MqttSubscription` handle for later cancellation.

        Raises:
            ConnectorError: If not connected or no topics configured.
        """
        self._ensure_connected()

        topic_cfgs = self._normalise_configs(configs) if configs else self._topic_configs
        if not topic_cfgs:
            raise ConnectorError("No topic configurations provided")

        # Subscribe to all topics on the broker
        for cfg in topic_cfgs:
            await self._client.subscribe(cfg.topic, qos=cfg.qos)
            logger.debug(
                "topic_subscribed",
                connector="MqttConnector",
                topic=cfg.topic,
                qos=cfg.qos,
                asset_id=cfg.asset_id,
            )

        # Register the subscription entry
        sub = MqttSubscription()
        entry = _SubscriptionEntry(callback=callback, topic_cfgs=topic_cfgs)
        self._sub_entries[sub.id] = entry
        self._subscriptions[sub.id] = sub

        # Start the shared reader if not already running
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name="mqtt-reader",
            )

        logger.info(
            "subscribed",
            connector="MqttConnector",
            subscription_id=sub.id,
            topic_count=len(topic_cfgs),
        )
        return sub

    async def unsubscribe(self, subscription: MqttSubscription) -> None:
        """Cancel an active MQTT subscription.

        Args:
            subscription: The handle returned by :meth:`subscribe`.
        """
        self._sub_entries.pop(subscription.id, None)
        self._subscriptions.pop(subscription.id, None)

        # Stop the reader if no subscriptions remain
        if not self._sub_entries and self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        logger.info(
            "unsubscribed",
            connector="MqttConnector",
            subscription_id=subscription.id,
        )

    async def publish(self, topic: str, payload: str | bytes, *, qos: int = 0) -> None:
        """Publish a message to an MQTT topic.

        Args:
            topic: The MQTT topic.
            payload: Message payload (string or bytes).
            qos: Quality of Service level (0, 1, or 2).

        Raises:
            ConnectorError: If not connected or publish fails.
        """
        self._ensure_connected()
        try:
            await self._client.publish(topic, payload=payload, qos=qos)
            logger.debug(
                "message_published",
                connector="MqttConnector",
                topic=topic,
                qos=qos,
            )
        except Exception as exc:
            raise ConnectorError(f"Failed to publish to {topic}: {exc}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise ConnectorError("Not connected — call connect() first")

    async def _reader_loop(self) -> None:
        """Single reader that fans out each message to all subscriptions.

        Only one ``_reader_loop`` runs per connector — it distributes
        incoming messages to every registered :class:`_SubscriptionEntry`.
        """
        try:
            async for message in self._client.messages:
                topic = str(message.topic)

                for entry in list(self._sub_entries.values()):
                    await self._dispatch_message(topic, message.payload, entry)
        except asyncio.CancelledError:
            return

    async def _dispatch_message(
        self,
        topic: str,
        payload: bytes,
        entry: _SubscriptionEntry,
    ) -> None:
        """Dispatch a single message to a subscription entry."""
        configs_by_topic = {cfg.topic: cfg for cfg in entry.topic_cfgs}
        cfg = self._match_topic(topic, configs_by_topic)
        if cfg is None:
            return

        try:
            value = self._parse_payload(payload, cfg)
        except Exception:
            logger.debug(
                "payload_parse_error",
                connector="MqttConnector",
                topic=topic,
                exc_info=True,
            )
            return

        if value is None:
            return

        # Only raise alarm when threshold is configured and exceeded
        if cfg.threshold and value <= cfg.threshold:
            return

        alarm = Alarm(
            id=f"ALM-{uuid.uuid4().hex[:8]}",
            asset_id=cfg.asset_id or topic,
            severity=cfg.severity,
            parameter=cfg.parameter or topic,
            value=value,
            threshold=cfg.threshold,
            unit=cfg.unit,
            timestamp=datetime.now(UTC),
            source=f"mqtt://{self._broker}/{topic}",
        )

        logger.info(
            "alarm_raised",
            connector="MqttConnector",
            alarm_id=alarm.id,
            asset_id=alarm.asset_id,
            parameter=alarm.parameter,
            value=alarm.value,
            threshold=alarm.threshold,
        )

        try:
            await entry.callback(alarm)
        except Exception:
            logger.error(
                "alarm_callback_error",
                connector="MqttConnector",
                alarm_id=alarm.id,
                exc_info=True,
            )

    @staticmethod
    def _match_topic(topic: str, configs: dict[str, TopicConfig]) -> TopicConfig | None:
        """Match an incoming topic to a config, supporting MQTT wildcards."""
        # Exact match first
        if topic in configs:
            return configs[topic]

        # Simple wildcard matching (# and +)
        for pattern, cfg in configs.items():
            if _mqtt_topic_matches(pattern, topic):
                return cfg
        return None

    @staticmethod
    def _parse_payload(payload: bytes, cfg: TopicConfig) -> float | None:
        """Extract a numeric value from an MQTT payload.

        Args:
            payload: Raw MQTT message bytes.
            cfg: Topic config with format and extraction settings.

        Returns:
            Extracted float value, or ``None`` if extraction fails.
        """
        fmt = (
            PayloadFormat(cfg.payload_format)
            if isinstance(cfg.payload_format, str)
            else cfg.payload_format
        )

        if fmt == PayloadFormat.JSON:
            return _parse_json_payload(payload, cfg.value_path)
        if fmt == PayloadFormat.SPARKPLUG_B:
            return _parse_sparkplug_payload(payload, cfg.parameter)
        if fmt == PayloadFormat.RAW:
            return _parse_raw_payload(payload)
        return None

    async def _cancel_subscription(self, sub: MqttSubscription) -> None:
        """Remove a subscription entry and stop the reader if empty."""
        self._sub_entries.pop(sub.id, None)
        if not self._sub_entries and self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

    @staticmethod
    def _normalise_configs(
        configs: list[TopicConfig | dict[str, Any]],
    ) -> list[TopicConfig]:
        """Convert mixed config dicts/dataclasses to TopicConfig list."""
        result: list[TopicConfig] = []
        for c in configs:
            if isinstance(c, dict):
                result.append(TopicConfig(**c))
            else:
                result.append(c)
        return result


# ------------------------------------------------------------------
# Payload parsing helpers
# ------------------------------------------------------------------


def _parse_json_payload(payload: bytes, value_path: str) -> float | None:
    """Extract a numeric value from a JSON payload using a dot-path key.

    Args:
        payload: Raw JSON bytes.
        value_path: Dot-separated path (e.g. ``"data.value"``).

    Returns:
        Extracted float or ``None``.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    # Walk the dot-path
    obj: Any = data
    for key in value_path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
        if obj is None:
            return None

    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _parse_sparkplug_payload(payload: bytes, parameter: str) -> float | None:
    """Parse a Sparkplug B message encoded as JSON.

    Sparkplug B messages are typically protobuf-encoded.  This function
    supports the common JSON representation used by MQTT-to-JSON bridges
    and Sparkplug B test tools::

        {"metrics": [{"name": "temperature", "value": 72.5, ...}, ...]}

    Args:
        payload: Raw message bytes (JSON-encoded Sparkplug B).
        parameter: Metric name to extract.

    Returns:
        Metric value as float, or ``None`` if not found.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    metrics = data.get("metrics", [])
    if not isinstance(metrics, list):
        return None

    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if metric.get("name") == parameter:
            try:
                return float(metric["value"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _parse_raw_payload(payload: bytes) -> float | None:
    """Interpret payload bytes as a plain numeric string.

    Args:
        payload: Raw bytes representing a number.

    Returns:
        Parsed float or ``None``.
    """
    try:
        return float(payload.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError):
        return None


def _mqtt_topic_matches(pattern: str, topic: str) -> bool:
    """Check if an MQTT topic matches a subscription pattern.

    Supports ``+`` (single-level) and ``#`` (multi-level) wildcards.

    Args:
        pattern: MQTT subscription filter.
        topic: Actual topic to test.

    Returns:
        ``True`` if the topic matches the pattern.
    """
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")

    for i, p in enumerate(pattern_parts):
        if p == "#":
            return True
        if i >= len(topic_parts):
            return False
        if p != "+" and p != topic_parts[i]:
            return False

    return len(pattern_parts) == len(topic_parts)
