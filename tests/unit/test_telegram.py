"""Tests for the TelegramConnector and CliChannel."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.comms.telegram import CliChannel, IncomingMessage, TelegramConnector
from machina.exceptions import ConnectorError


class TestIncomingMessage:
    """Test IncomingMessage data class."""

    def test_basic_message(self) -> None:
        msg = IncomingMessage("Hello", chat_id="123", user_name="Mario")
        assert msg.text == "Hello"
        assert msg.chat_id == "123"
        assert msg.user_name == "Mario"
        assert msg.channel == "telegram"

    def test_cli_channel_message(self) -> None:
        msg = IncomingMessage("Test", channel="cli", user_id="cli_user")
        assert msg.channel == "cli"

    def test_repr(self) -> None:
        msg = IncomingMessage("Hello world", user_name="Test")
        assert "Hello world" in repr(msg)


class TestTelegramConnector:
    """Test TelegramConnector (without actual Telegram connection)."""

    def test_capabilities(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        assert "send_message" in conn.capabilities
        assert "receive_message" in conn.capabilities

    @pytest.mark.asyncio
    async def test_connect_without_token_raises(self) -> None:
        conn = TelegramConnector()
        with pytest.raises(ConnectorError, match="bot_token"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.send_message("123", "test")

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_connect_with_mocked_telegram(self) -> None:
        """Connect with mocked telegram library."""
        mock_app = MagicMock()
        mock_builder_cls = MagicMock()
        mock_builder_cls.return_value.token.return_value.build.return_value = mock_app

        mock_telegram_ext = MagicMock()
        mock_telegram_ext.ApplicationBuilder = mock_builder_cls

        conn = TelegramConnector(bot_token="fake-token")

        with patch.dict(sys.modules, {"telegram.ext": mock_telegram_ext}):
            await conn.connect()

        assert conn._connected is True
        assert conn._application is mock_app

    @pytest.mark.asyncio
    async def test_connect_import_error(self) -> None:
        """Connect raises ImportError when telegram lib missing."""
        conn = TelegramConnector(bot_token="fake-token")

        # Ensure the telegram.ext module import raises
        with (
            patch.dict(sys.modules, {"telegram.ext": None}),
            pytest.raises(ImportError, match="python-telegram-bot"),
        ):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_disconnect_after_connect(self) -> None:
        """Disconnect after a mocked connect."""
        mock_app = AsyncMock()
        conn = TelegramConnector(bot_token="fake-token")
        conn._connected = True
        conn._application = mock_app

        await conn.disconnect()
        assert conn._connected is False
        assert conn._application is None

    @pytest.mark.asyncio
    async def test_health_check_connected(self) -> None:
        """Health check returns HEALTHY when connected."""
        conn = TelegramConnector(bot_token="fake-token")
        conn._connected = True
        health = await conn.health_check()
        assert health.status.value == "healthy"

    @pytest.mark.asyncio
    async def test_send_message_success(self) -> None:
        """send_message calls the bot's send_message."""
        mock_app = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        conn = TelegramConnector(bot_token="fake-token")
        conn._connected = True
        conn._application = mock_app

        await conn.send_message("12345", "Hello technician!")
        mock_app.bot.send_message.assert_awaited_once_with(chat_id=12345, text="Hello technician!")

    @pytest.mark.asyncio
    async def test_send_message_not_initialised(self) -> None:
        """send_message raises if application not initialised."""
        conn = TelegramConnector(bot_token="fake-token")
        conn._connected = True
        conn._application = None

        with pytest.raises(ConnectorError, match="not initialised"):
            await conn.send_message("123", "test")

    @pytest.mark.asyncio
    async def test_listen_not_connected(self) -> None:
        """listen raises if not connected."""
        conn = TelegramConnector(bot_token="fake-token")

        async def handler(msg: IncomingMessage) -> str:
            return "ok"

        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.listen(handler)

    def test_allowed_chat_ids(self) -> None:
        """Verify allowed_chat_ids is stored as a set."""
        conn = TelegramConnector(bot_token="fake", allowed_chat_ids=[111, 222])
        assert conn._allowed_chat_ids == {111, 222}

    def test_no_allowed_chat_ids(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        assert conn._allowed_chat_ids is None


class TestCliChannel:
    """Test CliChannel."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self) -> None:
        cli = CliChannel()
        await cli.connect()
        health = await cli.health_check()
        assert health.status.value == "healthy"
        await cli.disconnect()
        health = await cli.health_check()
        # CliChannel is always healthy by design
        assert health.status.value == "healthy"

    def test_capabilities(self) -> None:
        cli = CliChannel()
        assert "send_message" in cli.capabilities
        assert "receive_message" in cli.capabilities

    @pytest.mark.asyncio
    async def test_send_message_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        """send_message prints to stdout."""
        cli = CliChannel()
        await cli.send_message("cli", "Test response")
        captured = capsys.readouterr()
        assert "Test response" in captured.out

    @pytest.mark.asyncio
    async def test_listen_exit_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen exits cleanly on 'exit' command."""
        inputs = iter(["exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)
        # Handler should never have been called (exit was first input)
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_listen_with_message_then_exit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """listen processes message then exits."""
        inputs = iter(["hello", "quit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="Agent says hi")
        await cli.listen(handler)
        handler.assert_awaited_once()
        captured = capsys.readouterr()
        assert "Agent says hi" in captured.out

    @pytest.mark.asyncio
    async def test_listen_skips_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen skips empty lines."""
        inputs = iter(["", "  ", "bye"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_listen_eof_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen handles EOFError gracefully."""

        def _raise_eof(_prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)  # Should not raise

    def test_custom_prompt(self) -> None:
        cli = CliChannel(prompt=">>> ")
        assert cli._prompt == ">>> "

    @pytest.mark.asyncio
    async def test_listen_handler_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler returning empty string should not call send_message."""
        inputs = iter(["hello", "exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="")
        await cli.listen(handler)
        handler.assert_awaited_once()


class TestTelegramListen:
    """Test TelegramConnector.listen with fully mocked telegram library."""

    @pytest.mark.asyncio
    async def test_listen_not_initialised(self) -> None:
        """listen raises if application is None even when connected."""
        conn = TelegramConnector(bot_token="fake")
        conn._connected = True
        conn._application = None

        async def handler(msg: IncomingMessage) -> str:
            return "ok"

        with pytest.raises(ConnectorError, match="not initialised"):
            await conn.listen(handler)

    @pytest.mark.asyncio
    async def test_listen_full_flow(self) -> None:
        """Test full listen with mocked telegram module and CancelledError."""
        import asyncio

        # Build mock application
        mock_app = MagicMock()
        mock_app.add_handler = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_updater = MagicMock()
        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater

        conn = TelegramConnector(bot_token="fake")
        conn._connected = True
        conn._application = mock_app

        # Mock telegram imports
        mock_update = MagicMock()
        mock_filters = MagicMock()
        mock_context_types = MagicMock()
        mock_msg_handler_cls = MagicMock()

        mock_telegram = MagicMock()
        mock_telegram.Update = mock_update

        mock_telegram_ext = MagicMock()
        mock_telegram_ext.filters = mock_filters
        mock_telegram_ext.ContextTypes = mock_context_types
        mock_telegram_ext.MessageHandler = mock_msg_handler_cls

        # Make Event().wait() raise CancelledError immediately
        class _ImmediateCancelEvent:
            async def wait(self) -> None:
                raise asyncio.CancelledError

        async def handler(msg: IncomingMessage) -> str:
            return "response"

        with (
            patch.dict(
                sys.modules,
                {"telegram": mock_telegram, "telegram.ext": mock_telegram_ext},
            ),
            patch("asyncio.Event", _ImmediateCancelEvent),
        ):
            await conn.listen(handler)

        # Verify lifecycle
        mock_app.add_handler.assert_called_once()
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_updater.start_polling.assert_awaited_once()
        mock_updater.stop.assert_awaited_once()
        mock_app.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_message_callback(self) -> None:
        """Test the inner _on_message callback with mocked Update."""
        import asyncio

        # Build mock application
        mock_app = MagicMock()
        captured_handler = None

        def _capture_add_handler(h: MagicMock) -> None:
            nonlocal captured_handler
            captured_handler = h

        mock_app.add_handler = _capture_add_handler
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_updater = MagicMock()
        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater

        conn = TelegramConnector(bot_token="fake")
        conn._connected = True
        conn._application = mock_app

        # Mock telegram imports — capture the _on_message callback
        mock_telegram = MagicMock()
        mock_telegram_ext = MagicMock()
        captured_on_message = None

        def _fake_msg_handler(filter_expr: MagicMock, callback: MagicMock) -> MagicMock:
            nonlocal captured_on_message
            captured_on_message = callback
            return MagicMock()

        mock_telegram_ext.MessageHandler = _fake_msg_handler

        handler = AsyncMock(return_value="reply text")

        class _ImmediateCancelEvent:
            async def wait(self) -> None:
                raise asyncio.CancelledError

        with (
            patch.dict(
                sys.modules,
                {"telegram": mock_telegram, "telegram.ext": mock_telegram_ext},
            ),
            patch("asyncio.Event", _ImmediateCancelEvent),
        ):
            await conn.listen(handler)

        # Now invoke the captured _on_message callback
        assert captured_on_message is not None

        mock_update = MagicMock()
        mock_update.message.text = "Hello agent"
        mock_update.message.chat_id = 12345
        mock_update.message.from_user.id = 99
        mock_update.message.from_user.first_name = "Mario"
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()
        await captured_on_message(mock_update, mock_context)

        handler.assert_awaited_once()
        msg_arg = handler.call_args[0][0]
        assert msg_arg.text == "Hello agent"
        assert msg_arg.chat_id == "12345"
        assert msg_arg.user_name == "Mario"
        mock_update.message.reply_text.assert_awaited_once_with("reply text")

    @pytest.mark.asyncio
    async def test_on_message_no_message(self) -> None:
        """_on_message returns early when update.message is None."""
        import asyncio

        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_updater = MagicMock()
        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater

        conn = TelegramConnector(bot_token="fake")
        conn._connected = True
        conn._application = mock_app

        mock_telegram = MagicMock()
        mock_telegram_ext = MagicMock()
        captured_on_message = None

        def _fake_msg_handler(filter_expr: MagicMock, callback: MagicMock) -> MagicMock:
            nonlocal captured_on_message
            captured_on_message = callback
            return MagicMock()

        mock_telegram_ext.MessageHandler = _fake_msg_handler

        handler = AsyncMock(return_value="reply")

        class _ImmediateCancelEvent:
            async def wait(self) -> None:
                raise asyncio.CancelledError

        with (
            patch.dict(
                sys.modules,
                {"telegram": mock_telegram, "telegram.ext": mock_telegram_ext},
            ),
            patch("asyncio.Event", _ImmediateCancelEvent),
        ):
            await conn.listen(handler)

        assert captured_on_message is not None

        # Test with None message
        mock_update = MagicMock()
        mock_update.message = None
        await captured_on_message(mock_update, MagicMock())
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_message_unauthorized_chat(self) -> None:
        """_on_message ignores messages from unauthorized chats."""
        import asyncio

        mock_app = MagicMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_updater = MagicMock()
        mock_updater.start_polling = AsyncMock()
        mock_updater.stop = AsyncMock()
        mock_app.updater = mock_updater

        conn = TelegramConnector(bot_token="fake", allowed_chat_ids=[111])
        conn._connected = True
        conn._application = mock_app

        mock_telegram = MagicMock()
        mock_telegram_ext = MagicMock()
        captured_on_message = None

        def _fake_msg_handler(filter_expr: MagicMock, callback: MagicMock) -> MagicMock:
            nonlocal captured_on_message
            captured_on_message = callback
            return MagicMock()

        mock_telegram_ext.MessageHandler = _fake_msg_handler

        handler = AsyncMock(return_value="reply")

        class _ImmediateCancelEvent:
            async def wait(self) -> None:
                raise asyncio.CancelledError

        with (
            patch.dict(
                sys.modules,
                {"telegram": mock_telegram, "telegram.ext": mock_telegram_ext},
            ),
            patch("asyncio.Event", _ImmediateCancelEvent),
        ):
            await conn.listen(handler)

        assert captured_on_message is not None

        # Unauthorized chat_id = 999 (not in allowed_chat_ids [111])
        mock_update = MagicMock()
        mock_update.message.text = "Hello"
        mock_update.message.chat_id = 999
        await captured_on_message(mock_update, MagicMock())
        handler.assert_not_awaited()
