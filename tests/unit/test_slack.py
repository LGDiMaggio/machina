"""Tests for the SlackConnector."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.comms.slack import SlackConnector
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from machina.connectors.comms.telegram import IncomingMessage


class TestSlackConnector:
    """Test SlackConnector (without actual Slack connection)."""

    def test_capabilities(self) -> None:
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        assert "send_message" in conn.capabilities
        assert "receive_message" in conn.capabilities

    @pytest.mark.asyncio
    async def test_connect_without_bot_token_raises(self) -> None:
        conn = SlackConnector(app_token="xapp-fake")
        with pytest.raises(ConnectorError, match="bot_token"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_connect_without_app_token_raises(self) -> None:
        conn = SlackConnector(bot_token="xoxb-fake")
        with pytest.raises(ConnectorError, match="app_token"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.send_message("C123", "test")

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_check_connected(self) -> None:
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        health = await conn.health_check()
        assert health.status.value == "healthy"

    @pytest.mark.asyncio
    async def test_connect_with_mocked_slack(self) -> None:
        """Connect with mocked slack_bolt library."""
        mock_app = MagicMock()
        mock_async_app_cls = MagicMock(return_value=mock_app)

        mock_slack_bolt = MagicMock()
        mock_slack_bolt_async = MagicMock()
        mock_slack_bolt_async.AsyncApp = mock_async_app_cls

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")

        with patch.dict(
            sys.modules,
            {
                "slack_bolt": mock_slack_bolt,
                "slack_bolt.async_app": mock_slack_bolt_async,
            },
        ):
            await conn.connect()

        assert conn._connected is True
        assert conn._app is mock_app

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        """Connect raises ImportError when slack-bolt missing."""
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")

        with (
            patch.dict(sys.modules, {"slack_bolt.async_app": None}),
            pytest.raises(ImportError, match="slack-bolt"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_disconnect_after_connect(self) -> None:
        """Disconnect after a mocked connect."""
        mock_handler = AsyncMock()
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = MagicMock()
        conn._handler = mock_handler

        await conn.disconnect()
        assert conn._connected is False
        assert conn._app is None
        assert conn._handler is None

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self) -> None:
        """Disconnect when not connected is safe."""
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        await conn.disconnect()
        assert conn._connected is False

    @pytest.mark.asyncio
    async def test_send_message_success(self) -> None:
        """Send message via mocked Slack client."""
        mock_app = MagicMock()
        mock_app.client.chat_postMessage = AsyncMock()

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        await conn.send_message("C123", "Hello from Machina")
        mock_app.client.chat_postMessage.assert_awaited_once_with(
            channel="C123", text="Hello from Machina"
        )

    @pytest.mark.asyncio
    async def test_send_message_api_error(self) -> None:
        """Send message raises ConnectorError on Slack API failure."""
        mock_app = MagicMock()
        mock_app.client.chat_postMessage = AsyncMock(side_effect=Exception("API error"))

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        with pytest.raises(ConnectorError, match="Failed to send Slack message"):
            await conn.send_message("C123", "test")

    @pytest.mark.asyncio
    async def test_send_message_app_none_raises(self) -> None:
        """Send message raises when app is None."""
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = None

        with pytest.raises(ConnectorError, match="not initialised"):
            await conn.send_message("C123", "test")

    @pytest.mark.asyncio
    async def test_listen_app_none_raises(self) -> None:
        """Listen raises when app is None."""
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = None

        async def handler(msg: IncomingMessage) -> str:
            return "ok"

        with pytest.raises(ConnectorError, match="not initialised"):
            await conn.listen(handler)

    @pytest.mark.asyncio
    async def test_listen_import_error(self) -> None:
        """Listen raises ImportError when socket mode handler missing."""
        mock_app = MagicMock()
        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        async def handler(msg: IncomingMessage) -> str:
            return "ok"

        with (
            patch.dict(
                sys.modules,
                {"slack_bolt.adapter.socket_mode.async_handler": None},
            ),
            pytest.raises(ImportError, match="slack-bolt"),
        ):
            await conn.listen(handler)

    @pytest.mark.asyncio
    async def test_listen_registers_handler_and_starts(self) -> None:
        """Listen registers the message event handler and starts Socket Mode."""
        mock_app = MagicMock()
        registered_handlers: dict[str, Any] = {}

        def fake_event(event_type: str):
            def decorator(fn: Any) -> Any:
                registered_handlers[event_type] = fn
                return fn

            return decorator

        mock_app.event = fake_event

        mock_socket_handler = AsyncMock()
        mock_socket_handler.start_async = AsyncMock()
        mock_socket_handler.close_async = AsyncMock()

        mock_handler_cls = MagicMock(return_value=mock_socket_handler)

        mock_adapter_module = MagicMock()
        mock_adapter_module.AsyncSocketModeHandler = mock_handler_cls

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        async def handler(msg: IncomingMessage) -> str:
            return f"Echo: {msg.text}"

        # Make start_async cancel the event loop so listen() returns
        async def _start_and_cancel() -> None:
            # simulate brief listen then cancel
            raise asyncio.CancelledError

        # We need to mock the Event().wait() to cancel
        with (
            patch.dict(
                sys.modules,
                {"slack_bolt.adapter.socket_mode.async_handler": mock_adapter_module},
            ),
            patch("asyncio.Event") as mock_event_cls,
        ):
            mock_event = MagicMock()
            mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError)
            mock_event_cls.return_value = mock_event

            await conn.listen(handler)

        # Verify handler was registered and Socket Mode started
        assert "message" in registered_handlers
        mock_socket_handler.start_async.assert_awaited_once()
        mock_socket_handler.close_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_message_handler_creates_incoming_message(self) -> None:
        """The internal message handler creates a correct IncomingMessage."""
        mock_app = MagicMock()
        registered_handlers: dict[str, Any] = {}

        def fake_event(event_type: str):
            def decorator(fn: Any) -> Any:
                registered_handlers[event_type] = fn
                return fn

            return decorator

        mock_app.event = fake_event

        mock_socket_handler = AsyncMock()
        mock_handler_cls = MagicMock(return_value=mock_socket_handler)
        mock_adapter_module = MagicMock()
        mock_adapter_module.AsyncSocketModeHandler = mock_handler_cls

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        received_messages: list[IncomingMessage] = []

        async def handler(msg: IncomingMessage) -> str:
            received_messages.append(msg)
            return "ok"

        with (
            patch.dict(
                sys.modules,
                {"slack_bolt.adapter.socket_mode.async_handler": mock_adapter_module},
            ),
            patch("asyncio.Event") as mock_event_cls,
        ):
            mock_event = MagicMock()
            mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError)
            mock_event_cls.return_value = mock_event

            await conn.listen(handler)

        # Now call the registered handler directly
        say = AsyncMock()
        event = {
            "text": "Check pump P-201",
            "channel": "C123",
            "user": "U456",
        }
        on_message = registered_handlers["message"]
        await on_message(event=event, say=say)

        assert len(received_messages) == 1
        msg = received_messages[0]
        assert msg.text == "Check pump P-201"
        assert msg.chat_id == "C123"
        assert msg.user_id == "U456"
        assert msg.channel == "slack"
        say.assert_awaited_once_with("ok")

    @pytest.mark.asyncio
    async def test_message_handler_skips_subtypes(self) -> None:
        """Bot messages and edits (subtype != None) are skipped."""
        mock_app = MagicMock()
        registered_handlers: dict[str, Any] = {}

        def fake_event(event_type: str):
            def decorator(fn: Any) -> Any:
                registered_handlers[event_type] = fn
                return fn

            return decorator

        mock_app.event = fake_event

        mock_socket_handler = AsyncMock()
        mock_handler_cls = MagicMock(return_value=mock_socket_handler)
        mock_adapter_module = MagicMock()
        mock_adapter_module.AsyncSocketModeHandler = mock_handler_cls

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        received: list[IncomingMessage] = []

        async def handler(msg: IncomingMessage) -> str:
            received.append(msg)
            return "ok"

        with (
            patch.dict(
                sys.modules,
                {"slack_bolt.adapter.socket_mode.async_handler": mock_adapter_module},
            ),
            patch("asyncio.Event") as mock_event_cls,
        ):
            mock_event = MagicMock()
            mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError)
            mock_event_cls.return_value = mock_event
            await conn.listen(handler)

        # Subtype present → skip
        say = AsyncMock()
        event = {"text": "edited", "channel": "C1", "user": "U1", "subtype": "message_changed"}
        await registered_handlers["message"](event=event, say=say)
        assert len(received) == 0
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_message_handler_skips_unauthorized_channel(self) -> None:
        """Messages from non-whitelisted channels are skipped."""
        mock_app = MagicMock()
        registered_handlers: dict[str, Any] = {}

        def fake_event(event_type: str):
            def decorator(fn: Any) -> Any:
                registered_handlers[event_type] = fn
                return fn

            return decorator

        mock_app.event = fake_event

        mock_socket_handler = AsyncMock()
        mock_handler_cls = MagicMock(return_value=mock_socket_handler)
        mock_adapter_module = MagicMock()
        mock_adapter_module.AsyncSocketModeHandler = mock_handler_cls

        conn = SlackConnector(
            bot_token="xoxb-fake",
            app_token="xapp-fake",
            allowed_channel_ids=["C_ALLOWED"],
        )
        conn._connected = True
        conn._app = mock_app

        received: list[IncomingMessage] = []

        async def handler(msg: IncomingMessage) -> str:
            received.append(msg)
            return "ok"

        with (
            patch.dict(
                sys.modules,
                {"slack_bolt.adapter.socket_mode.async_handler": mock_adapter_module},
            ),
            patch("asyncio.Event") as mock_event_cls,
        ):
            mock_event = MagicMock()
            mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError)
            mock_event_cls.return_value = mock_event
            await conn.listen(handler)

        say = AsyncMock()
        # Unauthorized channel
        event = {"text": "hello", "channel": "C_OTHER", "user": "U1"}
        await registered_handlers["message"](event=event, say=say)
        assert len(received) == 0

        # Authorized channel
        event = {"text": "hello", "channel": "C_ALLOWED", "user": "U1"}
        await registered_handlers["message"](event=event, say=say)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_message_handler_empty_response(self) -> None:
        """Handler returning empty string should not call say()."""
        mock_app = MagicMock()
        registered_handlers: dict[str, Any] = {}

        def fake_event(event_type: str):
            def decorator(fn: Any) -> Any:
                registered_handlers[event_type] = fn
                return fn

            return decorator

        mock_app.event = fake_event

        mock_socket_handler = AsyncMock()
        mock_handler_cls = MagicMock(return_value=mock_socket_handler)
        mock_adapter_module = MagicMock()
        mock_adapter_module.AsyncSocketModeHandler = mock_handler_cls

        conn = SlackConnector(bot_token="xoxb-fake", app_token="xapp-fake")
        conn._connected = True
        conn._app = mock_app

        async def handler(msg: IncomingMessage) -> str:
            return ""

        with (
            patch.dict(
                sys.modules,
                {"slack_bolt.adapter.socket_mode.async_handler": mock_adapter_module},
            ),
            patch("asyncio.Event") as mock_event_cls,
        ):
            mock_event = MagicMock()
            mock_event.wait = AsyncMock(side_effect=asyncio.CancelledError)
            mock_event_cls.return_value = mock_event
            await conn.listen(handler)

        say = AsyncMock()
        event = {"text": "hello", "channel": "C1", "user": "U1"}
        await registered_handlers["message"](event=event, say=say)
        say.assert_not_awaited()
