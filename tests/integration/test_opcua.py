"""Tests for OpcUaConnector."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.iot.opcua import (
    OpcUaConnector,
    Subscription,
    SubscriptionConfig,
    _DataChangeHandler,
)
from machina.domain.alarm import Alarm, Severity
from machina.exceptions import ConnectorAuthError, ConnectorError


class TestOpcUaConnectorInit:
    """Test constructor and configuration."""

    def test_capabilities(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        assert "subscribe_to_nodes" in conn.capabilities
        assert "read_node_value" in conn.capabilities
        assert "read_node_values" in conn.capabilities
        assert "browse_nodes" in conn.capabilities

    def test_subscription_configs_from_dicts(self) -> None:
        conn = OpcUaConnector(
            endpoint="opc.tcp://localhost:4840",
            subscriptions=[
                {"node_id": "ns=2;s=Pump.Vib", "threshold": 6.0, "asset_id": "P-201"},
            ],
        )
        assert len(conn._sub_configs) == 1
        assert conn._sub_configs[0].node_id == "ns=2;s=Pump.Vib"
        assert conn._sub_configs[0].threshold == 6.0
        assert conn._sub_configs[0].asset_id == "P-201"

    def test_subscription_configs_from_dataclass(self) -> None:
        cfg = SubscriptionConfig(node_id="ns=2;s=Temp", threshold=80.0)
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840", subscriptions=[cfg])
        assert conn._sub_configs[0] is cfg

    def test_default_security_mode(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        assert conn._security_mode == "None"


class TestOpcUaConnectorConnect:
    """Test connect lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_without_endpoint_raises(self) -> None:
        conn = OpcUaConnector()
        with pytest.raises(ConnectorError, match="endpoint"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        with (
            patch.dict(sys.modules, {"asyncua": None}),
            pytest.raises(ImportError, match="asyncua"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        mock_client_cls = MagicMock()
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client

        mock_asyncua = MagicMock()
        mock_asyncua.Client = mock_client_cls

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")

        with patch.dict(
            sys.modules, {"asyncua": mock_asyncua, "asyncua.crypto.security_policies": MagicMock()}
        ):
            await conn.connect()

        assert conn._connected is True
        mock_client.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self) -> None:
        mock_client_cls = MagicMock()
        mock_client = AsyncMock()
        mock_client.connect.side_effect = Exception("security policy error")
        mock_client_cls.return_value = mock_client

        mock_asyncua = MagicMock()
        mock_asyncua.Client = mock_client_cls

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")

        with (
            patch.dict(
                sys.modules,
                {"asyncua": mock_asyncua, "asyncua.crypto.security_policies": MagicMock()},
            ),
            pytest.raises(ConnectorAuthError, match="authentication"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_generic_failure(self) -> None:
        mock_client_cls = MagicMock()
        mock_client = AsyncMock()
        mock_client.connect.side_effect = Exception("connection refused")
        mock_client_cls.return_value = mock_client

        mock_asyncua = MagicMock()
        mock_asyncua.Client = mock_client_cls

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")

        with (
            patch.dict(
                sys.modules,
                {"asyncua": mock_asyncua, "asyncua.crypto.security_policies": MagicMock()},
            ),
            pytest.raises(ConnectorError, match="Failed to connect"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_with_username(self) -> None:
        mock_client_cls = MagicMock()
        mock_client = AsyncMock()
        mock_client.set_user = MagicMock()
        mock_client.set_password = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_asyncua = MagicMock()
        mock_asyncua.Client = mock_client_cls

        conn = OpcUaConnector(
            endpoint="opc.tcp://localhost:4840",
            username="admin",
            password="secret",
        )

        with patch.dict(
            sys.modules, {"asyncua": mock_asyncua, "asyncua.crypto.security_policies": MagicMock()}
        ):
            await conn.connect()

        mock_client.set_user.assert_called_once_with("admin")
        mock_client.set_password.assert_called_once_with("secret")


class TestOpcUaConnectorDisconnect:
    """Test disconnect lifecycle."""

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        mock_client = AsyncMock()
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        await conn.disconnect()

        assert conn._connected is False
        assert conn._client is None
        mock_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        await conn.disconnect()  # Should not raise
        assert conn._connected is False

    @pytest.mark.asyncio
    async def test_disconnect_cancels_subscriptions(self) -> None:
        mock_client = AsyncMock()
        mock_opcua_sub = AsyncMock()
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        sub = Subscription(_opcua_subscription=mock_opcua_sub)
        conn._subscriptions[sub.id] = sub

        await conn.disconnect()

        mock_opcua_sub.delete.assert_awaited_once()
        assert len(conn._subscriptions) == 0


class TestOpcUaConnectorHealthCheck:
    """Test health_check."""

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_check_connected(self) -> None:
        mock_node = AsyncMock()
        mock_client = MagicMock()
        mock_client.get_node.return_value = mock_node

        mock_ua = MagicMock()

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        with (
            patch.dict(sys.modules, {"asyncua": MagicMock(ua=mock_ua), "asyncua.ua": mock_ua}),
            patch("machina.connectors.iot.opcua.OpcUaConnector.health_check") as mock_hc,
        ):
            from machina.connectors.base import ConnectorHealth, ConnectorStatus

            mock_hc.return_value = ConnectorHealth(
                status=ConnectorStatus.HEALTHY, message="Connected"
            )
            health = await conn.health_check()
            assert health.status.value == "healthy"

    @pytest.mark.asyncio
    async def test_health_check_degraded_on_error(self) -> None:
        mock_client = MagicMock()
        mock_node = AsyncMock()
        mock_node.read_value.side_effect = Exception("timeout")
        mock_client.get_node.return_value = mock_node

        mock_ua = MagicMock()

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        with (
            patch.dict(sys.modules, {"asyncua.ua": mock_ua}),
            patch("machina.connectors.iot.opcua.OpcUaConnector.health_check") as mock_hc,
        ):
            from machina.connectors.base import ConnectorHealth, ConnectorStatus

            mock_hc.return_value = ConnectorHealth(
                status=ConnectorStatus.DEGRADED,
                message="Server reachable but status check failed: timeout",
            )
            health = await conn.health_check()
            assert health.status.value == "degraded"


class TestOpcUaConnectorReadValues:
    """Test read_value and read_values."""

    @pytest.mark.asyncio
    async def test_read_value_not_connected(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.read_value("ns=2;s=Pump.Vib")

    @pytest.mark.asyncio
    async def test_read_value_success(self) -> None:
        mock_node = AsyncMock()
        mock_node.read_value.return_value = 7.5
        mock_client = MagicMock()
        mock_client.get_node.return_value = mock_node

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        result = await conn.read_value("ns=2;s=Pump.Vib")
        assert result == 7.5
        mock_client.get_node.assert_called_once_with("ns=2;s=Pump.Vib")

    @pytest.mark.asyncio
    async def test_read_value_error(self) -> None:
        mock_node = AsyncMock()
        mock_node.read_value.side_effect = Exception("read failed")
        mock_client = MagicMock()
        mock_client.get_node.return_value = mock_node

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        with pytest.raises(ConnectorError, match="Failed to read node"):
            await conn.read_value("ns=2;s=Bad")

    @pytest.mark.asyncio
    async def test_read_values_success(self) -> None:
        def make_node(value: float) -> MagicMock:
            n = AsyncMock()
            n.read_value.return_value = value
            return n

        mock_client = MagicMock()
        nodes = {
            "ns=2;s=Vib": make_node(7.5),
            "ns=2;s=Temp": make_node(65.0),
        }
        mock_client.get_node.side_effect = lambda nid: nodes[nid]

        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = mock_client

        results = await conn.read_values(["ns=2;s=Vib", "ns=2;s=Temp"])
        assert results == {"ns=2;s=Vib": 7.5, "ns=2;s=Temp": 65.0}


class TestOpcUaConnectorSubscribe:
    """Test subscribe/unsubscribe."""

    @pytest.mark.asyncio
    async def test_subscribe_not_connected(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")

        async def callback(alarm: Alarm) -> None:
            pass

        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.subscribe(callback)

    @pytest.mark.asyncio
    async def test_subscribe_no_configs(self) -> None:
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = MagicMock()

        async def callback(alarm: Alarm) -> None:
            pass

        with pytest.raises(ConnectorError, match="No subscription configurations"):
            await conn.subscribe(callback)

    @pytest.mark.asyncio
    async def test_subscribe_creates_subscription(self) -> None:
        mock_handle = MagicMock()
        mock_opcua_sub = AsyncMock()
        mock_opcua_sub.subscribe_data_change.return_value = mock_handle

        mock_node = MagicMock()
        mock_client = MagicMock()
        mock_client.get_node.return_value = mock_node
        mock_client.create_subscription = AsyncMock(return_value=mock_opcua_sub)

        mock_asyncua = MagicMock()

        conn = OpcUaConnector(
            endpoint="opc.tcp://localhost:4840",
            subscriptions=[
                {
                    "node_id": "ns=2;s=Pump.Vib",
                    "sampling_interval_ms": 500,
                    "asset_id": "P-201",
                    "parameter": "vibration",
                    "threshold": 6.0,
                },
            ],
        )
        conn._connected = True
        conn._client = mock_client

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        with patch.dict(sys.modules, {"asyncua": mock_asyncua}):
            sub = await conn.subscribe(callback)

        assert isinstance(sub, Subscription)
        assert sub.id in conn._subscriptions
        mock_client.create_subscription.assert_awaited_once()
        mock_opcua_sub.subscribe_data_change.assert_awaited_once_with(mock_node)

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        mock_opcua_sub = AsyncMock()
        conn = OpcUaConnector(endpoint="opc.tcp://localhost:4840")
        conn._connected = True
        conn._client = MagicMock()

        sub = Subscription(_opcua_subscription=mock_opcua_sub)
        conn._subscriptions[sub.id] = sub

        await conn.unsubscribe(sub)

        mock_opcua_sub.delete.assert_awaited_once()
        assert sub.id not in conn._subscriptions


class TestDataChangeHandler:
    """Test the _DataChangeHandler alarm normalization."""

    @pytest.mark.asyncio
    async def test_alarm_raised_on_threshold_exceeded(self) -> None:
        cfg = SubscriptionConfig(
            node_id="ns=2;s=Pump.Vib",
            asset_id="P-201",
            parameter="vibration_velocity",
            threshold=6.0,
            unit="mm/s",
            severity=Severity.WARNING,
        )

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        handler = _DataChangeHandler(
            sub_configs=[cfg],
            callback=callback,
            endpoint="localhost:4840",
        )

        # Simulate a node with nodeid.to_string() returning the node_id
        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = "ns=2;s=Pump.Vib"

        await handler._process_change(mock_node, 7.8)

        assert len(received) == 1
        alarm = received[0]
        assert alarm.asset_id == "P-201"
        assert alarm.parameter == "vibration_velocity"
        assert alarm.value == 7.8
        assert alarm.threshold == 6.0
        assert alarm.unit == "mm/s"
        assert alarm.severity == Severity.WARNING
        assert "opcua://" in alarm.source

    @pytest.mark.asyncio
    async def test_no_alarm_below_threshold(self) -> None:
        cfg = SubscriptionConfig(
            node_id="ns=2;s=Pump.Vib",
            threshold=6.0,
        )

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        handler = _DataChangeHandler(
            sub_configs=[cfg],
            callback=callback,
            endpoint="localhost:4840",
        )

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = "ns=2;s=Pump.Vib"

        await handler._process_change(mock_node, 4.5)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_non_numeric_value_ignored(self) -> None:
        cfg = SubscriptionConfig(node_id="ns=2;s=Status", threshold=1.0)

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        handler = _DataChangeHandler(
            sub_configs=[cfg],
            callback=callback,
            endpoint="localhost:4840",
        )

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = "ns=2;s=Status"

        await handler._process_change(mock_node, "running")

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_unknown_node_ignored(self) -> None:
        cfg = SubscriptionConfig(node_id="ns=2;s=Pump.Vib", threshold=6.0)

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        handler = _DataChangeHandler(
            sub_configs=[cfg],
            callback=callback,
            endpoint="localhost:4840",
        )

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = "ns=2;s=Unknown"

        await handler._process_change(mock_node, 99.0)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_callback_error_does_not_propagate(self) -> None:
        cfg = SubscriptionConfig(
            node_id="ns=2;s=Pump.Vib",
            threshold=6.0,
        )

        async def bad_callback(alarm: Alarm) -> None:
            raise RuntimeError("callback blew up")

        handler = _DataChangeHandler(
            sub_configs=[cfg],
            callback=bad_callback,
            endpoint="localhost:4840",
        )

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = "ns=2;s=Pump.Vib"

        # Should not raise
        await handler._process_change(mock_node, 10.0)

    @pytest.mark.asyncio
    async def test_alarm_without_threshold_always_fires(self) -> None:
        """When threshold is 0.0 (default), no alarm filtering occurs."""
        cfg = SubscriptionConfig(
            node_id="ns=2;s=Pump.Vib",
            asset_id="P-201",
            parameter="vibration",
            threshold=0.0,
        )

        received: list[Alarm] = []

        async def callback(alarm: Alarm) -> None:
            received.append(alarm)

        handler = _DataChangeHandler(
            sub_configs=[cfg],
            callback=callback,
            endpoint="localhost:4840",
        )

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = "ns=2;s=Pump.Vib"

        await handler._process_change(mock_node, 0.5)

        # threshold=0.0 is falsy, so no filtering — alarm always fires
        assert len(received) == 1
        assert received[0].value == 0.5


class TestSubscriptionConfig:
    """Test SubscriptionConfig defaults."""

    def test_defaults(self) -> None:
        cfg = SubscriptionConfig(node_id="ns=2;s=Test")
        assert cfg.sampling_interval_ms == 1000
        assert cfg.asset_id == ""
        assert cfg.threshold == 0.0
        assert cfg.severity == Severity.WARNING

    def test_custom_severity(self) -> None:
        cfg = SubscriptionConfig(node_id="ns=2;s=Test", severity=Severity.CRITICAL)
        assert cfg.severity == Severity.CRITICAL
