"""Tests for the TelegramConnector and IncomingMessage."""

from __future__ import annotations

import contextlib
import io
import logging
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.connectors.comms.cli import CliChannel
from machina.connectors.comms.telegram import (
    _TOKEN_LOGGERS,
    IncomingMessage,
    TelegramConnector,
    _RedactToken,
)
from machina.connectors.comms.types import (
    SupportsConfirmation,
    supports_sync_confirmation,
)
from machina.exceptions import ConnectorAuthError, ConnectorError
from machina.observability.logging import configure_logging, traceback_text

if TYPE_CHECKING:
    from collections.abc import Iterator


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


class TestConfirmationCapabilityDetection:
    """Test the SupportsConfirmation protocol / supports_sync_confirmation helper."""

    def test_cli_channel_is_sync_capable(self) -> None:
        cli = CliChannel()
        assert supports_sync_confirmation(cli) is True
        assert isinstance(cli, SupportsConfirmation)

    def test_telegram_connector_needs_two_turn(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        assert supports_sync_confirmation(conn) is False
        assert not isinstance(conn, SupportsConfirmation)

    def test_plain_object_needs_two_turn(self) -> None:
        assert supports_sync_confirmation(object()) is False


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


_TOKEN = "123456789:AAFakeTokenValue_ShouldNeverBeLogged0"


class _InvalidToken(Exception):  # noqa: N818 — mirrors the library's class name
    """Stands in for ``telegram.error.InvalidToken``."""


def _telegram_modules() -> dict[str, Any]:
    error_module = ModuleType("telegram.error")
    error_module.InvalidToken = _InvalidToken  # type: ignore[attr-defined]
    return {"telegram": MagicMock(), "telegram.ext": MagicMock(), "telegram.error": error_module}


def _rejecting_app(error: Exception) -> MagicMock:
    app = MagicMock()
    app.initialize = AsyncMock(side_effect=error)
    return app


async def _no_reply(msg: IncomingMessage) -> str:
    return ""


class TestBotTokenStaysOutOfLogs:
    """The bot token never reaches an exception message or a log line."""

    @pytest.mark.asyncio
    async def test_rejected_token_raises_without_the_token(self) -> None:
        """python-telegram-bot's own message names the token; ours does not."""
        conn = TelegramConnector(bot_token=_TOKEN)
        conn._connected = True
        conn._application = _rejecting_app(
            _InvalidToken(f"The token `{_TOKEN}` was rejected by the server.")
        )

        with (
            patch.dict(sys.modules, _telegram_modules()),
            pytest.raises(ConnectorAuthError, match="rejected the bot token") as exc_info,
        ):
            await conn.listen(_no_reply)

        assert _TOKEN not in str(exc_info.value)
        assert _TOKEN not in traceback_text(exc_info.value)
        # Nothing on the exception chain still holds the original.
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None

    @pytest.mark.asyncio
    async def test_startup_error_naming_the_token_is_withheld(self) -> None:
        """An error body that repeats the request URL carries the token too."""
        conn = TelegramConnector(bot_token=_TOKEN)
        conn._connected = True
        conn._application = _rejecting_app(
            RuntimeError(f"<html>blocked: https://api.telegram.org/bot{_TOKEN}/getMe</html>")
        )

        with (
            patch.dict(sys.modules, _telegram_modules()),
            pytest.raises(ConnectorError, match="RuntimeError") as exc_info,
        ):
            await conn.listen(_no_reply)

        assert _TOKEN not in traceback_text(exc_info.value)
        assert exc_info.value.__context__ is None

    def test_surrounding_whitespace_is_stripped_from_the_token(self) -> None:
        """In a request URL it would be percent-encoded, out of the filter's reach."""
        assert TelegramConnector(bot_token=f" {_TOKEN}\n")._bot_token == _TOKEN

    @pytest.mark.asyncio
    async def test_other_startup_failures_propagate_unchanged(self) -> None:
        conn = TelegramConnector(bot_token=_TOKEN)
        conn._connected = True
        failure = RuntimeError("network unreachable")
        conn._application = _rejecting_app(failure)

        with patch.dict(sys.modules, _telegram_modules()), pytest.raises(RuntimeError) as exc_info:
            await conn.listen(_no_reply)

        assert exc_info.value is failure

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _TOKEN_LOGGERS)
    async def test_token_loggers_redact_the_token(self, name: str) -> None:
        """Each logger that can print the token, during and after the connection.

        httpx logs every request URL at INFO and python-telegram-bot logs its
        API URLs at DEBUG; both URLs hold the token. Polling can outlive
        ``disconnect()``, so the redaction does too.
        """
        conn = TelegramConnector(bot_token=_TOKEN)
        with _captured_token_loggers() as records:
            with patch.dict(sys.modules, {"telegram.ext": MagicMock()}):
                await conn.connect()
            logging.getLogger(name).info(
                'HTTP Request: %s %s "%s"', "POST", f"https://api.telegram.org/bot{_TOKEN}/x", "OK"
            )
            await conn.disconnect()
            logging.getLogger(name).info("after disconnect %s", _TOKEN)

        during, after = (r.getMessage() for r in records)
        assert during == 'HTTP Request: POST https://api.telegram.org/bot<redacted>/x "OK"'
        assert after == "after disconnect <redacted>"

    @pytest.mark.asyncio
    async def test_redaction_is_in_place_before_the_bot_is_built(self) -> None:
        """Building the bot already logs its API URL ("Set Bot API URL: ...")."""

        def _build() -> MagicMock:
            logging.getLogger("telegram.ext.ExtBot").debug(
                "Set Bot API URL: %s", f"https://api.telegram.org/bot{_TOKEN}"
            )
            return MagicMock()

        ext = MagicMock()
        ext.ApplicationBuilder.return_value.token.return_value.build.side_effect = _build
        with _captured_token_loggers() as records, patch.dict(sys.modules, {"telegram.ext": ext}):
            await TelegramConnector(bot_token=_TOKEN).connect()

        assert [r.getMessage() for r in records] == [
            "Set Bot API URL: https://api.telegram.org/bot<redacted>"
        ]

    def test_a_malformed_record_passes_through(self) -> None:
        record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "%s %s", ("one",), None)

        assert _RedactToken(_TOKEN).filter(record) is True

    @pytest.mark.asyncio
    async def test_rejected_token_reaches_no_log_line_with_the_real_library(self) -> None:
        """End to end: python-telegram-bot, httpx and ``configure_logging(level="DEBUG")``.

        Every Bot API call answers 401, as for a revoked token. The token must
        appear in none of: python-telegram-bot's DEBUG URL lines, httpx's
        request line, or the exception a host logs when ``agent.run()`` fails.
        """
        pytest.importorskip("telegram")
        import httpx
        import structlog

        async def _unauthorized(self: Any, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )

        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        out = io.StringIO()
        try:
            with _open_token_loggers():
                configure_logging(level="DEBUG", json_output=True)
                # configure_logging() keeps httpx at WARNING; an application
                # that lowers it again must still get its request line redacted.
                logging.getLogger("httpx").setLevel(logging.NOTSET)
                root.handlers[0].stream = out  # type: ignore[attr-defined]
                with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _unauthorized):
                    conn = TelegramConnector(bot_token=_TOKEN)
                    await conn.connect()
                    with pytest.raises(ConnectorAuthError) as exc_info:
                        await conn.listen(_no_reply)
                    logging.getLogger("host").error("agent_failed", exc_info=exc_info.value)
                    await conn.disconnect()
        finally:
            structlog.reset_defaults()
            root.handlers[:] = handlers
            root.setLevel(level)

        logged = out.getvalue()
        # Each line that used to carry the token was logged, without it.
        assert "Set Bot API URL: https://api.telegram.org/bot<redacted>" in logged
        assert "https://api.telegram.org/bot<redacted>/getMe" in logged
        assert "ConnectorAuthError" in logged
        assert _TOKEN not in logged


@contextlib.contextmanager
def _open_token_loggers() -> Iterator[None]:
    """Pin the token loggers open, whatever an earlier test left behind.

    A ``logging.config.dictConfig`` call elsewhere in the suite disables
    existing loggers, and a level set on one would silence it: either makes a
    "no token in the log" assertion pass vacuously.
    """
    loggers = [logging.getLogger(name) for name in _TOKEN_LOGGERS]
    saved = [(lg.disabled, lg.level, lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.disabled, lg.propagate = False, True
        lg.setLevel(logging.NOTSET)
    try:
        yield
    finally:
        for lg, (disabled, level, propagate) in zip(loggers, saved, strict=True):
            lg.disabled, lg.propagate = disabled, propagate
            lg.setLevel(level)


@contextlib.contextmanager
def _captured_token_loggers() -> Iterator[list[logging.LogRecord]]:
    """Collect every record the token loggers emit at DEBUG and above."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    loggers = [logging.getLogger(name) for name in _TOKEN_LOGGERS]
    with _open_token_loggers():
        for lg in loggers:
            lg.setLevel(logging.DEBUG)
            lg.addHandler(handler)
        try:
            yield records
        finally:
            for lg in loggers:
                lg.removeHandler(handler)
