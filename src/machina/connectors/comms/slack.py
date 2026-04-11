"""SlackConnector — Slack integration for maintenance team communication.

Provides a communication channel between maintenance teams and the Machina
agent via Slack.  Uses the ``slack-bolt`` SDK in **Socket Mode** for
bidirectional messaging behind firewalls without a public endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.comms.types import IncomingMessage, MessageHandler
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


class SlackConnector:
    """Connector for Slack via the Bolt SDK (Socket Mode).

    Receives messages from technicians in Slack channels and sends
    responses back.  Socket Mode uses a WebSocket connection so no
    public URL or ingress is required — ideal for on-premise plants.

    Args:
        bot_token: Slack Bot User OAuth Token (``xoxb-…``).
        app_token: Slack App-Level Token (``xapp-…``) for Socket Mode.
        allowed_channel_ids: Optional whitelist of channel IDs that may interact.

    Example:
        ```python
        from machina.connectors import Slack

        slack = Slack(bot_token="xoxb-...", app_token="xapp-...")
        await slack.connect()

        async def handler(msg):
            return f"Echo: {msg.text}"

        await slack.listen(handler)
        ```
    """

    capabilities: ClassVar[list[str]] = ["send_message", "receive_message"]

    def __init__(
        self,
        *,
        bot_token: str = "",
        app_token: str = "",
        allowed_channel_ids: list[str] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed_channel_ids = set(allowed_channel_ids) if allowed_channel_ids else None
        self._connected = False
        self._app: Any = None
        self._handler: Any = None  # Socket Mode handler

    async def connect(self) -> None:
        """Validate tokens and initialise the Slack Bolt application."""
        if not self._bot_token:
            raise ConnectorError("bot_token is required for SlackConnector")
        if not self._app_token:
            raise ConnectorError("app_token is required for SlackConnector (Socket Mode)")

        try:
            from slack_bolt.async_app import AsyncApp

            self._app = AsyncApp(token=self._bot_token)
            logger.info("connected", connector="SlackConnector")
        except ImportError:
            msg = (
                "slack-bolt is required for SlackConnector. "
                "Install with: pip install machina-ai[slack]"
            )
            raise ImportError(msg) from None
        self._connected = True

    async def disconnect(self) -> None:
        """Shut down the Slack Socket Mode handler."""
        if self._handler is not None:
            with contextlib.suppress(Exception):
                await self._handler.close_async()
        self._handler = None
        self._app = None
        self._connected = False
        logger.info("disconnected", connector="SlackConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check Slack connectivity."""
        if not self._connected:
            return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message="Not connected")
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="Connected")

    async def send_message(self, channel: str, text: str) -> None:
        """Send a message to a Slack channel or DM.

        Args:
            channel: Slack channel ID (e.g. ``C0123456789``) or user ID for DMs.
            text: Message text to send.

        Raises:
            ConnectorError: If not connected or the Slack API call fails.
        """
        self._ensure_connected()
        if self._app is None:
            raise ConnectorError("Slack application not initialised")

        try:
            await self._app.client.chat_postMessage(channel=channel, text=text)
            logger.debug(
                "message_sent",
                connector="SlackConnector",
                channel=channel,
            )
        except Exception as exc:
            raise ConnectorError(f"Failed to send Slack message: {exc}") from exc

    async def listen(self, handler: MessageHandler) -> None:
        """Start listening for incoming Slack messages via Socket Mode.

        This is a blocking call that maintains the WebSocket connection
        until cancelled.

        Args:
            handler: Async callback that receives an :class:`IncomingMessage`
                     and returns the response text.
        """
        self._ensure_connected()
        if self._app is None:
            raise ConnectorError("Slack application not initialised")

        try:
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
        except ImportError:
            msg = (
                "slack-bolt is required for SlackConnector. "
                "Install with: pip install machina-ai[slack]"
            )
            raise ImportError(msg) from None

        @self._app.event("message")  # type: ignore[untyped-decorator]
        async def _on_message(event: dict[str, Any], say: Any) -> None:
            # Skip bot messages and message edits
            if event.get("subtype") is not None:
                return

            channel_id = event.get("channel", "")
            if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
                logger.warning(
                    "unauthorized_channel",
                    connector="SlackConnector",
                    channel=channel_id,
                )
                return

            msg = IncomingMessage(
                text=event.get("text", ""),
                chat_id=channel_id,
                user_id=event.get("user", ""),
                user_name=event.get("user", ""),
                channel="slack",
                raw=event,
            )

            logger.info(
                "message_received",
                connector="SlackConnector",
                user=msg.user_id,
                channel=msg.chat_id,
            )

            response = await handler(msg)
            if response:
                await say(response)

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        logger.info("listening", connector="SlackConnector")
        await self._handler.start_async()

        # Keep alive until cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._handler.close_async()

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")
