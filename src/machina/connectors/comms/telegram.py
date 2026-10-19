"""TelegramConnector — Telegram Bot API integration for technician interaction.

Provides a communication channel between maintenance technicians and the
Machina agent via Telegram.  Uses ``python-telegram-bot`` under the hood.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus, sandbox_aware
from machina.connectors.capabilities import Capability
from machina.connectors.comms.types import IncomingMessage, MessageHandler
from machina.exceptions import ConnectorAuthError, ConnectorError

logger = structlog.get_logger(__name__)

# "CliChannel" stays in __all__ for backwards compatibility (it moved to
# machina.connectors.comms.cli; the published import path keeps working) but
# resolves lazily via __getattr__ below, so importing telegram.py does not
# force cli.py to load.

__all__ = ["CliChannel", "IncomingMessage", "MessageHandler", "TelegramConnector"]  # noqa: F822


def __getattr__(name: str) -> Any:
    """Lazy back-compat re-export of ``CliChannel`` (PEP 562)."""
    if name == "CliChannel":
        from machina.connectors.comms.cli import CliChannel

        return CliChannel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _RedactToken(logging.Filter):
    """Replace one bot token in the log records a logger emits.

    The Bot API puts the token in every request URL
    (``https://api.telegram.org/bot<token>/getUpdates``). httpx logs each
    request URL at INFO, so under ``configure_logging()`` every poll would
    write the token to the log.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is the logging module's to report
            return True
        if self._token in message:
            record.msg = message.replace(self._token, "<redacted>")
            record.args = ()
        return True


# The loggers that can print the bot token:
# - httpx logs every request URL at INFO;
# - python-telegram-bot logs its API URLs at DEBUG while the bot is built
#   ("Set Bot API URL: ..."), and an error body that repeats the URL at ERROR.
# A logger's filter does not apply to records from its child loggers, so each
# logger is named.
_TOKEN_LOGGERS = (
    "httpx",
    "telegram.Bot",
    "telegram.ext.ExtBot",
    "telegram.ext.Application",
    "telegram.ext.Updater",
    "telegram.request.BaseRequest",
)
_redacted_tokens: set[str] = set()


def _redact_token_in_logs(token: str) -> None:
    """Filter *token* out of every logger in ``_TOKEN_LOGGERS``, once per token.

    The filters stay for the life of the process: python-telegram-bot keeps
    polling when ``disconnect()`` is called while ``listen()`` runs
    (``Application.shutdown()`` refuses then), and a filter that rewrites only
    this exact token changes nothing else.
    """
    if token in _redacted_tokens:
        return
    _redacted_tokens.add(token)
    redact = _RedactToken(token)
    for name in _TOKEN_LOGGERS:
        logging.getLogger(name).addFilter(redact)


class TelegramConnector:
    """Connector for Telegram Bot API.

    Receives messages from technicians via Telegram and sends responses
    back.  When used with the agent runtime, incoming messages are routed
    through the agent's reasoning pipeline.

    Args:
        bot_token: Telegram bot token from BotFather. Surrounding whitespace
            is stripped: the Bot API would reject it, and in a request URL
            it is percent-encoded, which hides the token from log redaction.
        allowed_chat_ids: Optional whitelist of chat IDs that may interact.

    Example:
        ```python
        telegram = TelegramConnector(bot_token="123:ABC...")
        await telegram.connect()

        async def handler(msg: IncomingMessage) -> str:
            return f"Echo: {msg.text}"

        await telegram.listen(handler)
        ```
    """

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.SEND_MESSAGE, Capability.RECEIVE_MESSAGE}
    )

    def __init__(
        self,
        *,
        bot_token: str = "",
        allowed_chat_ids: list[int] | None = None,
    ) -> None:
        self._bot_token = bot_token.strip()
        self._allowed_chat_ids = set(allowed_chat_ids) if allowed_chat_ids else None
        self._connected = False
        self._application: Any = None

    async def connect(self) -> None:
        """Validate bot token and prepare the Telegram application."""
        if not self._bot_token:
            raise ConnectorError("bot_token is required for TelegramConnector")

        # Before build(): building the bot already logs its API URLs.
        _redact_token_in_logs(self._bot_token)
        try:
            from telegram.ext import (  # type: ignore[import-not-found,unused-ignore]
                ApplicationBuilder,
            )

            self._application = ApplicationBuilder().token(self._bot_token).build()
            logger.info("connected", connector="TelegramConnector")
        except ImportError:
            msg = (
                "python-telegram-bot is required for TelegramConnector. "
                "Install with: pip install machina-ai[telegram]"
            )
            raise ImportError(msg) from None
        self._connected = True

    async def disconnect(self) -> None:
        """Shut down the Telegram bot."""
        if self._application is not None:
            with contextlib.suppress(Exception):
                await self._application.shutdown()
        self._application = None
        self._connected = False
        logger.info("disconnected", connector="TelegramConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check bot connectivity."""
        if not self._connected:
            return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message="Not connected")
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="Connected")

    @sandbox_aware
    async def send_message(self, chat_id: str | int, text: str) -> None:
        """Send a message to a Telegram chat.

        Args:
            chat_id: The Telegram chat ID.
            text: Message text to send.

        Raises:
            ConnectorError: If not connected or application not initialised.
            SandboxViolationError: If sandbox mode is active.
        """
        self._ensure_connected()
        if self._application is None:
            raise ConnectorError("Telegram application not initialised")

        await self._application.bot.send_message(
            chat_id=int(chat_id),
            text=text,
        )
        logger.debug(
            "message_sent",
            connector="TelegramConnector",
            chat_id=chat_id,
        )

    async def listen(self, handler: MessageHandler) -> None:
        """Start listening for incoming messages and dispatch to handler.

        This is a blocking call that runs the Telegram polling loop.

        Args:
            handler: Async callback that receives an :class:`IncomingMessage`
                     and returns the response text.
        """
        self._ensure_connected()
        if self._application is None:
            raise ConnectorError("Telegram application not initialised")

        from telegram import Update  # type: ignore[import-not-found,unused-ignore]  # noqa: TC002
        from telegram.ext import ContextTypes, filters
        from telegram.ext import MessageHandler as TGMsgHandler

        async def _on_message(
            update: Update,
            context: ContextTypes.DEFAULT_TYPE,
        ) -> None:
            if update.message is None or update.message.text is None:
                return

            chat_id = update.message.chat_id
            if self._allowed_chat_ids and chat_id not in self._allowed_chat_ids:
                logger.warning(
                    "unauthorized_chat",
                    connector="TelegramConnector",
                    chat_id=chat_id,
                )
                return

            msg = IncomingMessage(
                text=update.message.text,
                chat_id=str(chat_id),
                user_id=str(update.message.from_user.id) if update.message.from_user else "",
                user_name=(
                    update.message.from_user.first_name if update.message.from_user else ""
                ),
                channel="telegram",
                raw=update,
            )

            logger.info(
                "message_received",
                connector="TelegramConnector",
                user=msg.user_name,
                chat_id=msg.chat_id,
            )

            response = await handler(msg)
            if response:
                await update.message.reply_text(response)

        self._application.add_handler(TGMsgHandler(filters.TEXT & ~filters.COMMAND, _on_message))

        logger.info("listening", connector="TelegramConnector")
        withheld: ConnectorError | None = None
        try:
            await self._application.initialize()
        except Exception as exc:
            from telegram.error import (  # type: ignore[import-not-found,unused-ignore]
                InvalidToken,
            )

            # python-telegram-bot puts a rejected token in the message ("The
            # token `<token>` was rejected by the server."), and an error body
            # that repeats the request URL carries it too.
            if isinstance(exc, InvalidToken):
                withheld = ConnectorAuthError("Telegram rejected the bot token")
            elif self._bot_token in str(exc):
                withheld = ConnectorError(
                    f"Telegram start-up failed with {type(exc).__name__}; "
                    "its message named the bot token and is withheld"
                )
            else:
                raise
        if withheld is not None:
            # Raised outside the except block, so the original exception is
            # neither chained nor kept as __context__: no traceback, log line
            # or code walking the exception chain reaches the token.
            raise withheld
        await self._application.start()
        await self._application.updater.start_polling()

        # Keep alive until cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._application.updater.stop()
            await self._application.stop()

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")
