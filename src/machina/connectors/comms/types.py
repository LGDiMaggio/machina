"""Shared types for communication connectors.

Defines :class:`IncomingMessage` and :data:`MessageHandler` — the
common message type and handler callback signature used by all
communication connectors (Telegram, Slack, Email, CLI).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass
class IncomingMessage:
    """A message received from a communication channel.

    Args:
        text: The message text.
        chat_id: Identifier for the chat/conversation.
        user_id: Identifier for the sender.
        user_name: Display name of the sender.
        channel: Channel type (``"telegram"``, ``"slack"``, ``"email"``, ``"cli"``).
        raw: Raw platform-specific message object.

    Example:
        ```python
        from machina.connectors.comms import IncomingMessage

        msg = IncomingMessage("Check pump P-201", chat_id="123", user_name="Mario")
        ```
    """

    text: str
    chat_id: str = ""
    user_id: str = ""
    user_name: str = ""
    channel: str = "telegram"
    raw: Any = None

    def __repr__(self) -> str:
        return (
            f"IncomingMessage(channel={self.channel!r}, "
            f"user={self.user_name!r}, text={self.text[:50]!r})"
        )


# Type alias for the handler callback
MessageHandler = Callable[[IncomingMessage], Coroutine[Any, Any, str]]

__all__ = ["IncomingMessage", "MessageHandler"]
