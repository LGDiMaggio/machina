"""TelegramConnector — Telegram Bot API integration for technician interaction.

Provides a communication channel between maintenance technicians and the
Machina agent via Telegram.  Uses ``python-telegram-bot`` under the hood.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


class IncomingMessage:
    """A message received from a communication channel.

    Attributes:
        text: The message text.
        chat_id: Identifier for the chat/conversation.
        user_id: Identifier for the sender.
        user_name: Display name of the sender.
        channel: Channel type (``"telegram"`` or ``"cli"``).
        raw: Raw platform-specific message object.
    """

    __slots__ = ("channel", "chat_id", "raw", "text", "user_id", "user_name")

    def __init__(
        self,
        text: str,
        *,
        chat_id: str = "",
        user_id: str = "",
        user_name: str = "",
        channel: str = "telegram",
        raw: Any = None,
    ) -> None:
        self.text = text
        self.chat_id = chat_id
        self.user_id = user_id
        self.user_name = user_name
        self.channel = channel
        self.raw = raw

    def __repr__(self) -> str:
        return (
            f"IncomingMessage(channel={self.channel!r}, "
            f"user={self.user_name!r}, text={self.text[:50]!r})"
        )


# Type alias for the handler callback
MessageHandler = Callable[[IncomingMessage], Coroutine[Any, Any, str]]


class TelegramConnector:
    """Connector for Telegram Bot API.

    Receives messages from technicians via Telegram and sends responses
    back.  When used with the agent runtime, incoming messages are routed
    through the agent's reasoning pipeline.

    Args:
        bot_token: Telegram bot token from BotFather.
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

    capabilities: ClassVar[list[str]] = ["send_message", "receive_message"]

    def __init__(
        self,
        *,
        bot_token: str = "",
        allowed_chat_ids: list[int] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._allowed_chat_ids = set(allowed_chat_ids) if allowed_chat_ids else None
        self._connected = False
        self._application: Any = None

    async def connect(self) -> None:
        """Validate bot token and prepare the Telegram application."""
        if not self._bot_token:
            raise ConnectorError("bot_token is required for TelegramConnector")

        try:
            from telegram.ext import ApplicationBuilder

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
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY, message="Not connected"
            )
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="Connected")

    async def send_message(self, chat_id: str | int, text: str) -> None:
        """Send a message to a Telegram chat.

        Args:
            chat_id: The Telegram chat ID.
            text: Message text to send.
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

        from telegram import Update  # noqa: TC002
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
        await self._application.initialize()
        await self._application.start()
        await self._application.updater.start_polling()  # type: ignore[union-attr]

        # Keep alive until cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._application.updater.stop()  # type: ignore[union-attr]
            await self._application.stop()

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")


class CliChannel:
    """Interactive command-line channel for testing without Telegram.

    Provides the same interface as TelegramConnector so the agent
    runtime can use it as a drop-in replacement.

    Example:
        ```python
        cli = CliChannel()
        await cli.connect()

        async def handler(msg: IncomingMessage) -> str:
            return f"Agent: {msg.text}"

        await cli.listen(handler)
        ```
    """

    capabilities: ClassVar[list[str]] = ["send_message", "receive_message"]

    def __init__(self, *, prompt: str = "You> ") -> None:
        self._prompt = prompt
        self._connected = False

    async def connect(self) -> None:
        """No-op for CLI mode."""
        self._connected = True
        logger.info("connected", connector="CliChannel")

    async def disconnect(self) -> None:
        """No-op for CLI mode."""
        self._connected = False
        logger.info("disconnected", connector="CliChannel")

    async def health_check(self) -> ConnectorHealth:
        """Always healthy."""
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="CLI mode")

    async def send_message(self, chat_id: str | int, text: str) -> None:
        """Print message to stdout."""
        print(f"\n🤖 {text}")

    async def listen(self, handler: MessageHandler) -> None:
        """Read from stdin in a loop and dispatch to handler.

        Args:
            handler: Async callback that receives an :class:`IncomingMessage`
                     and returns the response text.
        """
        print("\n╔══════════════════════════════════════════╗")
        print("║  Machina Agent — CLI Mode                ║")
        print("║  Type your questions. Ctrl+C to exit.    ║")
        print("╚══════════════════════════════════════════╝\n")

        loop = asyncio.get_event_loop()
        try:
            while True:
                text = await loop.run_in_executor(None, lambda: input(self._prompt))
                text = text.strip()
                if not text:
                    continue
                if text.lower() in ("exit", "quit", "bye"):
                    print("👋 Goodbye!")
                    break

                msg = IncomingMessage(
                    text=text,
                    chat_id="cli",
                    user_id="cli_user",
                    user_name="User",
                    channel="cli",
                )
                response = await handler(msg)
                if response:
                    await self.send_message("cli", response)
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Goodbye!")
