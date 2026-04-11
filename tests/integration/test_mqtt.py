"""Tests for MqttConnector."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.iot.mqtt import (
    MqttConnector,
    MqttSubscription,
    PayloadFormat,
    TopicConfig,
    _mqtt_topic_matches,
    _parse_json_payload,
    _parse_raw_payload,
    _parse_sparkplug_payload,
)
from machina.domain.alarm import Alarm, Severity
from machina.exceptions import ConnectorAuthError, ConnectorError


class TestMqttConnectorInit:
    """Test constructor and configuration."""

    def test_capabilities(self) -> None:
        conn = MqttConnector(broker="localhost")
        assert "subscribe_to_topics" in conn.capabilities
        assert "publish_message" in conn.capabilities

    def test_topic_configs_from_dicts(self) -> None:
        conn = MqttConnector(
            broker="localhost",
            topics=[
                {"topic": "plant/sensors/vib", "threshold": 6.0, "asset_id": "P-201"},
            ],
        )
        assert len(conn._topic_configs) == 1
        assert conn._topic_configs[0].topic == "plant/sensors/vib"
        assert conn._topic_configs[0].threshold == 6.0

    def test_topic_configs_from_dataclass(self) -> None:
        cfg = TopicConfig(topic="plant/sensors/vib")
        conn = MqttConnector(broker="localhost", topics=[cfg])
        assert conn._topic_configs[0] is cfg

    def test_default_port(self) -> None:
        conn = MqttConnector(broker="localhost")
        assert conn._port == 1883

    def test_auto_generated_client_id(self) -> None:
        conn = MqttConnector(broker="localhost")
        assert conn._client_id.startswith("machina-")

    def test_custom_client_id(self) -> None:
        conn = MqttConnector(broker="localhost", client_id="my-agent")
        assert conn._client_id == "my-agent"


class TestMqttConnectorConnect:
    """Test connect lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_without_broker_raises(self) -> None:
        conn = MqttConnector()
        with pytest.raises(ConnectorError, match="broker"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        conn = MqttConnector(broker="localhost")
        with (
            patch.dict(sys.modules, {"aiomqtt": None}),
            pytest.raises(ImportError, match="aiomqtt"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        mock_client_instance = AsyncMock()
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_aiomqtt = MagicMock()
        mock_aiomqtt.Client = mock_client_cls

        conn = MqttConnector(broker="localhost")

        with patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}):
            await conn.connect()

        assert conn._connected is True

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self) -> None:
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("not authorized")
        )

        mock_aiomqtt = MagicMock()
        mock_aiomqtt.Client = mock_client_cls

        conn = MqttConnector(broker="localhost", username="bad", password="creds")

        with (
            patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}),
            pytest.raises(ConnectorAuthError, match="authentication"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_generic_failure(self) -> None:
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("connection refused")
        )

        mock_aiomqtt = MagicMock()
        mock_aiomqtt.Client = mock_client_cls

        conn = MqttConnector(broker="localhost")

        with (
            patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}),
            pytest.raises(ConnectorError, match="Failed to connect"),
        ):
            await conn.connect()


class TestMqttConnectorDisconnect:
    """Test disconnect lifecycle."""

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        mock_cm = AsyncMock()
        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = MagicMock()
        conn._client_cm = mock_cm

        await conn.disconnect()

        assert conn._connected is False
        assert conn._client is None
        mock_cm.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self) -> None:
        conn = MqttConnector(broker="localhost")
        await conn.disconnect()  # Should not raise
        assert conn._connected is False

    @pytest.mark.asyncio
    async def test_disconnect_cancels_reader_task(self) -> None:
        # Create a real task that we can cancel and await
        async def _hang() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_hang())

        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = MagicMock()
        conn._client_cm = AsyncMock()
        conn._reader_task = task

        sub = MqttSubscription()
        conn._subscriptions[sub.id] = sub

        await conn.disconnect()
        assert task.cancelled()
        assert len(conn._subscriptions) == 0
        assert conn._reader_task is None


class TestMqttConnectorHealthCheck:
    """Test health_check."""

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = MqttConnector(broker="localhost")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_check_connected(self) -> None:
        conn = MqttConnector(broker="mqtt.example.com", port=8883)
        conn._connected = True
        conn._client = MagicMock()

        health = await conn.health_check()
        assert health.status.value == "healthy"
        assert health.details["broker"] == "mqtt.example.com"
        assert health.details["port"] == 8883


class TestMqttConnectorSubscribe:
    """Test subscribe/unsubscribe."""

    @pytest.mark.asyncio
    async def test_subscribe_not_connected(self) -> None:
        conn = MqttConnector(broker="localhost")

        async def callback(alarm: Alarm) -> None:
            pass

        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.subscribe(callback)

    @pytest.mark.asyncio
    async def test_subscribe_no_configs(self) -> None:
        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = MagicMock()

        async def callback(alarm: Alarm) -> None:
            pass

        with pytest.raises(ConnectorError, match="No topic configurations"):
            await conn.subscribe(callback)

    @pytest.mark.asyncio
    async def test_subscribe_creates_reader_task(self) -> None:
        mock_client = AsyncMock()
        conn = MqttConnector(
            broker="localhost",
            topics=[{"topic": "sensors/#", "threshold": 6.0}],
        )
        conn._connected = True
        conn._client = mock_client

        async def callback(alarm: Alarm) -> None:
            pass

        sub = await conn.subscribe(callback)

        assert isinstance(sub, MqttSubscription)
        assert sub.id in conn._subscriptions
        assert conn._reader_task is not None
        mock_client.subscribe.assert_awaited_once_with("sensors/#", qos=0)

        # Cleanup
        conn._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conn._reader_task

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_reader_when_empty(self) -> None:
        # Create a real task that simulates the reader
        async def _hang() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_hang())

        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = MagicMock()
        conn._reader_task = task

        sub = MqttSubscription()
        conn._subscriptions[sub.id] = sub
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        async def _cb(alarm: Alarm) -> None:
            pass

        conn._sub_entries[sub.id] = _SubscriptionEntry(callback=_cb, topic_cfgs=[])

        await conn.unsubscribe(sub)
        assert task.cancelled()
        assert sub.id not in conn._subscriptions
        assert conn._reader_task is None


class TestMqttConnectorPublish:
    """Test publish."""

    @pytest.mark.asyncio
    async def test_publish_not_connected(self) -> None:
        conn = MqttConnector(broker="localhost")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.publish("test/topic", "hello")

    @pytest.mark.asyncio
    async def test_publish_success(self) -> None:
        mock_client = AsyncMock()
        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = mock_client

        await conn.publish("test/topic", "hello", qos=1)
        mock_client.publish.assert_awaited_once_with("test/topic", payload="hello", qos=1)

    @pytest.mark.asyncio
    async def test_publish_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.publish.side_effect = Exception("publish failed")
        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = mock_client

        with pytest.raises(ConnectorError, match="Failed to publish"):
            await conn.publish("test/topic", "hello")


# ------------------------------------------------------------------
# Payload parsing tests
# ------------------------------------------------------------------


class TestParseJsonPayload:
    """Test JSON payload parsing."""

    def test_simple_value(self) -> None:
        payload = json.dumps({"value": 12.5}).encode()
        assert _parse_json_payload(payload, "value") == 12.5

    def test_nested_value(self) -> None:
        payload = json.dumps({"data": {"value": 7.8}}).encode()
        assert _parse_json_payload(payload, "data.value") == 7.8

    def test_deeply_nested(self) -> None:
        payload = json.dumps({"a": {"b": {"c": 99.1}}}).encode()
        assert _parse_json_payload(payload, "a.b.c") == 99.1

    def test_missing_key(self) -> None:
        payload = json.dumps({"other": 5}).encode()
        assert _parse_json_payload(payload, "value") is None

    def test_non_numeric_value(self) -> None:
        payload = json.dumps({"value": "running"}).encode()
        assert _parse_json_payload(payload, "value") is None

    def test_invalid_json(self) -> None:
        assert _parse_json_payload(b"not json", "value") is None

    def test_integer_value_as_float(self) -> None:
        payload = json.dumps({"value": 42}).encode()
        assert _parse_json_payload(payload, "value") == 42.0


class TestParseSparkplugPayload:
    """Test Sparkplug B payload parsing."""

    def test_metric_found(self) -> None:
        payload = json.dumps({"metrics": [{"name": "temperature", "value": 72.5}]}).encode()
        assert _parse_sparkplug_payload(payload, "temperature") == 72.5

    def test_metric_not_found(self) -> None:
        payload = json.dumps({"metrics": [{"name": "temperature", "value": 72.5}]}).encode()
        assert _parse_sparkplug_payload(payload, "pressure") is None

    def test_multiple_metrics(self) -> None:
        payload = json.dumps(
            {
                "metrics": [
                    {"name": "temperature", "value": 72.5},
                    {"name": "vibration", "value": 4.2},
                    {"name": "pressure", "value": 101.3},
                ]
            }
        ).encode()
        assert _parse_sparkplug_payload(payload, "vibration") == 4.2

    def test_empty_metrics(self) -> None:
        payload = json.dumps({"metrics": []}).encode()
        assert _parse_sparkplug_payload(payload, "temperature") is None

    def test_invalid_json(self) -> None:
        assert _parse_sparkplug_payload(b"not json", "temp") is None

    def test_no_metrics_key(self) -> None:
        payload = json.dumps({"data": 123}).encode()
        assert _parse_sparkplug_payload(payload, "temp") is None


class TestParseRawPayload:
    """Test raw payload parsing."""

    def test_numeric_string(self) -> None:
        assert _parse_raw_payload(b"12.5") == 12.5

    def test_integer_string(self) -> None:
        assert _parse_raw_payload(b"42") == 42.0

    def test_whitespace(self) -> None:
        assert _parse_raw_payload(b"  7.8  \n") == 7.8

    def test_non_numeric(self) -> None:
        assert _parse_raw_payload(b"hello") is None

    def test_empty(self) -> None:
        assert _parse_raw_payload(b"") is None


# ------------------------------------------------------------------
# MQTT topic matching tests
# ------------------------------------------------------------------


class TestMqttTopicMatching:
    """Test MQTT wildcard topic matching."""

    def test_exact_match(self) -> None:
        assert _mqtt_topic_matches("a/b/c", "a/b/c") is True

    def test_exact_no_match(self) -> None:
        assert _mqtt_topic_matches("a/b/c", "a/b/d") is False

    def test_single_level_wildcard(self) -> None:
        assert _mqtt_topic_matches("a/+/c", "a/b/c") is True
        assert _mqtt_topic_matches("a/+/c", "a/x/c") is True
        assert _mqtt_topic_matches("a/+/c", "a/b/d") is False

    def test_multi_level_wildcard(self) -> None:
        assert _mqtt_topic_matches("a/#", "a/b") is True
        assert _mqtt_topic_matches("a/#", "a/b/c") is True
        assert _mqtt_topic_matches("a/#", "a/b/c/d") is True

    def test_root_multi_level(self) -> None:
        assert _mqtt_topic_matches("#", "a/b/c") is True

    def test_length_mismatch(self) -> None:
        assert _mqtt_topic_matches("a/b", "a/b/c") is False
        assert _mqtt_topic_matches("a/b/c", "a/b") is False


class TestMqttConnectorTls:
    """Test TLS configuration."""

    @pytest.mark.asyncio
    async def test_connect_with_tls(self) -> None:
        """TLS enabled creates an ssl context and passes it to aiomqtt.Client."""
        mock_client_instance = AsyncMock()
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_aiomqtt = MagicMock()
        mock_aiomqtt.Client = mock_client_cls

        conn = MqttConnector(broker="mqtt.example.com", port=8883, tls=True)

        import ssl as real_ssl

        with (
            patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}),
            patch.object(real_ssl, "create_default_context") as mock_create_ctx,
        ):
            mock_create_ctx.return_value = MagicMock()
            await conn.connect()

        assert conn._connected is True
        mock_create_ctx.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_with_tls_and_ca_certs(self) -> None:
        """TLS with custom CA certs loads them into the context."""
        mock_client_instance = AsyncMock()
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_aiomqtt = MagicMock()
        mock_aiomqtt.Client = mock_client_cls

        mock_ctx = MagicMock()

        conn = MqttConnector(
            broker="mqtt.example.com",
            port=8883,
            tls=True,
            ca_certs="/path/to/ca.pem",
        )

        import ssl as real_ssl

        with (
            patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}),
            patch.object(real_ssl, "create_default_context", return_value=mock_ctx),
        ):
            await conn.connect()

        mock_ctx.load_verify_locations.assert_called_once_with("/path/to/ca.pem")

    @pytest.mark.asyncio
    async def test_connect_without_tls_no_ssl(self) -> None:
        """When tls=False, no SSL context is created."""
        mock_client_instance = AsyncMock()
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_aiomqtt = MagicMock()
        mock_aiomqtt.Client = mock_client_cls

        conn = MqttConnector(broker="localhost", port=1883, tls=False)

        with patch.dict(sys.modules, {"aiomqtt": mock_aiomqtt}):
            await conn.connect()

        # Client was created with tls_context=None
        call_kwargs = mock_client_cls.call_args[1]
        assert call_kwargs["tls_context"] is None


class TestMqttConnectorMultipleTopics:
    """Test multiple simultaneous topic subscriptions."""

    @pytest.mark.asyncio
    async def test_subscribe_multiple_topics(self) -> None:
        """Subscribing with multiple topics should subscribe to all of them."""
        mock_client = AsyncMock()
        conn = MqttConnector(
            broker="localhost",
            topics=[
                {"topic": "plant/pump/vib", "threshold": 6.0},
                {"topic": "plant/pump/temp", "threshold": 80.0},
                {"topic": "plant/comp/vib", "threshold": 4.0},
            ],
        )
        conn._connected = True
        conn._client = mock_client

        async def callback(alarm: Alarm) -> None:
            pass

        await conn.subscribe(callback)

        assert mock_client.subscribe.await_count == 3
        topics_subscribed = [call.args[0] for call in mock_client.subscribe.await_args_list]
        assert "plant/pump/vib" in topics_subscribed
        assert "plant/pump/temp" in topics_subscribed
        assert "plant/comp/vib" in topics_subscribed

        # Cleanup
        conn._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conn._reader_task

    @pytest.mark.asyncio
    async def test_subscribe_with_custom_qos(self) -> None:
        """QoS levels from TopicConfig should be passed to MQTT subscribe."""
        mock_client = AsyncMock()
        conn = MqttConnector(
            broker="localhost",
            topics=[
                TopicConfig(topic="important/data", qos=2, threshold=1.0),
            ],
        )
        conn._connected = True
        conn._client = mock_client

        async def callback(alarm: Alarm) -> None:
            pass

        await conn.subscribe(callback)
        mock_client.subscribe.assert_awaited_once_with("important/data", qos=2)

        conn._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conn._reader_task


class TestTopicConfig:
    """Test TopicConfig defaults."""

    def test_defaults(self) -> None:
        cfg = TopicConfig(topic="test/topic")
        assert cfg.qos == 0
        assert cfg.threshold == 0.0
        assert cfg.payload_format == PayloadFormat.JSON
        assert cfg.value_path == "value"
        assert cfg.severity == Severity.WARNING

    def test_custom_format(self) -> None:
        cfg = TopicConfig(topic="test", payload_format=PayloadFormat.SPARKPLUG_B)
        assert cfg.payload_format == PayloadFormat.SPARKPLUG_B

    def test_string_format(self) -> None:
        cfg = TopicConfig(topic="test", payload_format="raw")
        assert cfg.payload_format == "raw"


# ---------------------------------------------------------------------------
# Dispatch message & alarm creation
# ---------------------------------------------------------------------------


class TestMqttDispatchMessage:
    """Test _dispatch_message, alarm creation, and callback execution."""

    @pytest.mark.asyncio
    async def test_dispatch_creates_alarm_on_threshold_exceeded(self) -> None:
        """dispatch_message creates an Alarm when value > threshold."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(
            broker="localhost",
            topics=[TopicConfig(topic="sensor/temp", threshold=50.0, parameter="temperature")],
        )

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[TopicConfig(topic="sensor/temp", threshold=50.0, parameter="temperature")],
        )

        payload = json.dumps({"value": 75.0}).encode()
        await conn._dispatch_message("sensor/temp", payload, entry)

        assert len(received) == 1
        assert received[0].value == 75.0
        assert received[0].parameter == "temperature"

    @pytest.mark.asyncio
    async def test_dispatch_skips_below_threshold(self) -> None:
        """dispatch_message skips alarm when value <= threshold."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[TopicConfig(topic="sensor/temp", threshold=50.0)],
        )

        payload = json.dumps({"value": 30.0}).encode()
        await conn._dispatch_message("sensor/temp", payload, entry)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_dispatch_skips_unmatched_topic(self) -> None:
        """dispatch_message skips when topic doesn't match any config."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[TopicConfig(topic="sensor/pressure", threshold=100.0)],
        )

        payload = json.dumps({"value": 200.0}).encode()
        await conn._dispatch_message("other/topic", payload, entry)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_dispatch_handles_parse_error(self) -> None:
        """dispatch_message gracefully handles unparseable payloads."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[TopicConfig(topic="sensor/temp", threshold=0.0)],
        )

        await conn._dispatch_message("sensor/temp", b"not-valid-json", entry)
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_dispatch_handles_callback_error(self) -> None:
        """dispatch_message logs callback errors but doesn't crash."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        async def bad_callback(alarm: Alarm) -> None:
            raise RuntimeError("Handler crashed")

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=bad_callback,
            topic_cfgs=[TopicConfig(topic="sensor/temp", threshold=0.0)],
        )

        # Should not raise
        payload = json.dumps({"value": 100.0}).encode()
        await conn._dispatch_message("sensor/temp", payload, entry)

    @pytest.mark.asyncio
    async def test_dispatch_sparkplug_payload(self) -> None:
        """dispatch_message handles Sparkplug B format."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[
                TopicConfig(
                    topic="spBv1.0/group/DDATA/node/device",
                    threshold=0.0,
                    payload_format=PayloadFormat.SPARKPLUG_B,
                    parameter="vibration",
                )
            ],
        )

        sparkplug_data = json.dumps({"metrics": [{"name": "vibration", "value": 12.5}]}).encode()
        await conn._dispatch_message("spBv1.0/group/DDATA/node/device", sparkplug_data, entry)

        assert len(received) == 1
        assert received[0].value == 12.5

    @pytest.mark.asyncio
    async def test_dispatch_raw_payload(self) -> None:
        """dispatch_message handles raw numeric payloads."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[
                TopicConfig(
                    topic="sensor/raw",
                    threshold=0.0,
                    payload_format=PayloadFormat.RAW,
                )
            ],
        )

        await conn._dispatch_message("sensor/raw", b"42.5", entry)
        assert len(received) == 1
        assert received[0].value == 42.5


# ---------------------------------------------------------------------------
# Cancel subscription & normalise configs
# ---------------------------------------------------------------------------


class TestMqttSubscriptionManagement:
    """Test subscription cancellation and config normalisation."""

    @pytest.mark.asyncio
    async def test_cancel_subscription_removes_entry(self) -> None:
        """_cancel_subscription removes the entry from _sub_entries."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        conn = MqttConnector(broker="localhost", topics=[])

        async def callback(alarm: Alarm) -> None:
            pass

        sub = MqttSubscription(id="sub-1")
        conn._sub_entries["sub-1"] = _SubscriptionEntry(callback=callback, topic_cfgs=[])
        conn._reader_task = None

        await conn._cancel_subscription(sub)
        assert "sub-1" not in conn._sub_entries

    @pytest.mark.asyncio
    async def test_cancel_last_subscription_stops_reader(self) -> None:
        """When the last subscription is cancelled, the reader task is stopped."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        conn = MqttConnector(broker="localhost", topics=[])

        async def callback(alarm: Alarm) -> None:
            pass

        async def _hang() -> None:
            await asyncio.Event().wait()

        sub = MqttSubscription(id="sub-1")
        conn._sub_entries["sub-1"] = _SubscriptionEntry(callback=callback, topic_cfgs=[])
        conn._reader_task = asyncio.create_task(_hang())

        await conn._cancel_subscription(sub)
        assert conn._reader_task is None

    def test_normalise_configs_from_dicts(self) -> None:
        """_normalise_configs converts dicts to TopicConfig objects."""
        configs = MqttConnector._normalise_configs(
            [
                {"topic": "t/1", "threshold": 10.0},
                TopicConfig(topic="t/2"),
            ]
        )
        assert len(configs) == 2
        assert isinstance(configs[0], TopicConfig)
        assert configs[0].topic == "t/1"
        assert configs[0].threshold == 10.0
        assert isinstance(configs[1], TopicConfig)

    def test_parse_payload_json_invalid_returns_none(self) -> None:
        """_parse_payload returns None for non-JSON data with JSON format."""
        conn = MqttConnector(broker="localhost", topics=[])
        cfg = TopicConfig(topic="t", payload_format=PayloadFormat.JSON)
        result = conn._parse_payload(b"not-json-{{{", cfg)
        assert result is None

    def test_parse_payload_sparkplug_no_matching_metric(self) -> None:
        """_parse_payload returns None when no metric matches."""
        conn = MqttConnector(broker="localhost", topics=[])
        cfg = TopicConfig(
            topic="t",
            payload_format=PayloadFormat.SPARKPLUG_B,
            parameter="nonexistent",
        )
        data = json.dumps({"metrics": [{"name": "other", "value": 1.0}]}).encode()
        result = conn._parse_payload(data, cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_no_threshold_always_alarms(self) -> None:
        """When threshold is 0, any positive value triggers alarm."""
        from machina.connectors.iot.mqtt import _SubscriptionEntry

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        conn = MqttConnector(broker="localhost", topics=[])

        entry = _SubscriptionEntry(
            callback=callback,
            topic_cfgs=[TopicConfig(topic="sensor/x", threshold=0.0)],
        )

        # threshold=0 means the condition `value <= threshold` is 0.0 <= 0.0 = True → skip
        # Actually with threshold=0 and value > 0, it passes
        payload = json.dumps({"value": 0.1}).encode()
        await conn._dispatch_message("sensor/x", payload, entry)
        assert len(received) == 1
