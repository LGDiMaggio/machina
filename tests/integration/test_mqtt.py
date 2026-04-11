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
    async def test_disconnect_cancels_subscriptions(self) -> None:
        # Create a real task that we can cancel and await
        async def _hang() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_hang())

        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = MagicMock()
        conn._client_cm = AsyncMock()

        sub = MqttSubscription(_task=task)
        conn._subscriptions[sub.id] = sub

        await conn.disconnect()
        assert task.cancelled()
        assert len(conn._subscriptions) == 0


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
    async def test_subscribe_creates_task(self) -> None:
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
        assert sub._task is not None
        mock_client.subscribe.assert_awaited_once_with("sensors/#", qos=0)

        # Cleanup
        sub._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sub._task

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        # Create a real task that we can cancel and await
        async def _hang() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_hang())

        conn = MqttConnector(broker="localhost")
        conn._connected = True
        conn._client = MagicMock()

        sub = MqttSubscription(_task=task)
        conn._subscriptions[sub.id] = sub

        await conn.unsubscribe(sub)
        assert task.cancelled()
        assert sub.id not in conn._subscriptions


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
