"""CliChannel — interactive command-line channel for local agent sessions.

A lightweight local substitute for
:class:`~machina.connectors.comms.telegram.TelegramConnector`: it exposes the
same channel interface (``send_message``, ``listen``, ``request_confirmation``)
over stdin/stdout so agents can be tested without a Telegram bot token.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.connectors.comms.types import IncomingMessage, MessageHandler, is_affirmation

logger = structlog.get_logger(__name__)

__all__ = ["CliChannel"]


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

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.SEND_MESSAGE, Capability.RECEIVE_MESSAGE}
    )

    def __init__(self, *, prompt: str = "You> ") -> None:
        self._prompt = prompt
        self._connected = False

    async def connect(self) -> None:
        """No-op for CLI mode."""
        self._connected = True
        logger.info("connected", connector="CliChannel", operation="connect")

    async def disconnect(self) -> None:
        """No-op for CLI mode."""
        self._connected = False
        logger.info("disconnected", connector="CliChannel", operation="disconnect")

    async def health_check(self) -> ConnectorHealth:
        """Always healthy."""
        return ConnectorHealth(status=ConnectorStatus.HEALTHY, message="CLI mode")

    async def send_message(self, chat_id: str | int, text: str) -> None:
        """Print message to stdout."""
        print(f"\n🤖 {text}")

    async def request_confirmation(self, chat_id: str | int, prompt: str) -> bool:
        """Render a pending-write prompt and read a ``[y/N]`` decision.

        Implements the :class:`~machina.connectors.comms.types.SupportsConfirmation`
        seam synchronously for CLI use: the runtime-built ``prompt`` (which
        already states the concrete proposed write) is printed, then a single
        line is read from stdin via the same ``run_in_executor(None, input)``
        pattern :meth:`listen` uses, so the agent loop can pause for input
        without blocking the event loop.

        Args:
            chat_id: Identifier for the chat (unused for CLI; kept for the
                protocol signature).
            prompt: Human-readable confirmation text describing the write.

        Returns:
            ``True`` only when the user types an affirmative answer (one of the
            shared English/Italian affirmation tokens — ``y`` / ``yes`` / ``ok``
            / ``sì`` / ``conferma`` …, case-insensitive, trimmed, whole-message
            only); ``False`` for an empty line or anything not affirmative
            (safe default — never auto-confirm). Uses the same
            :func:`~machina.connectors.comms.types.is_affirmation` grammar as
            the runtime's two-turn confirmation path, so CLI and async channels
            accept identical answers.
        """
        print(f"\n⚠️  {prompt}")

        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(None, lambda: input("Confirm? [y/N] "))
        return is_affirmation(answer)

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

        loop = asyncio.get_running_loop()
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
