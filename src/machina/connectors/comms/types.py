"""Shared types for communication connectors.

Defines :class:`IncomingMessage` and :data:`MessageHandler` — the
common message type and handler callback signature used by all
communication connectors (Telegram, Slack, Email, CLI).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


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


@runtime_checkable
class SupportsConfirmation(Protocol):
    """Channels that can confirm a pending write synchronously, in-turn.

    This is the per-channel confirmation seam used by the agent runtime's
    human-in-the-loop write gate.  A channel that implements
    :meth:`request_confirmation` can collect a yes/no decision *within the
    same turn* (e.g. ``CliChannel`` reads a ``[y/N]`` line from stdin).
    Channels that do **not** implement it (e.g. :class:`TelegramConnector`
    and other async channels) signal — by their absence of the method —
    that they require the two-turn propose→confirm degrade.

    The runtime always builds the human-readable confirmation text (which
    states the concrete proposed write); the channel only renders that
    text and returns the user's decision.  Shaping confirmation as a
    per-channel method means a future rendering (e.g. native inline
    buttons) can be added as another implementation of this protocol
    without changing the runtime.

    The protocol is ``runtime_checkable`` so the runtime can detect
    support with :func:`isinstance` / :func:`supports_sync_confirmation`
    without importing any channel implementation (and its optional
    transport dependencies).
    """

    async def request_confirmation(self, chat_id: str | int, prompt: str) -> bool:
        """Ask the user to confirm a pending write and return their decision.

        Args:
            chat_id: Identifier for the chat/conversation to prompt in.
            prompt: Human-readable confirmation text built by the runtime,
                already stating the concrete proposed write.

        Returns:
            ``True`` if the user explicitly affirmed; ``False`` otherwise
            (including empty/ambiguous input — never auto-confirm).
        """
        ...


def supports_sync_confirmation(channel: object) -> bool:
    """Return whether a channel can confirm a write synchronously.

    A channel is sync-capable when it provides a callable
    ``request_confirmation`` method (the :class:`SupportsConfirmation`
    seam).  The runtime uses this to choose between the synchronous gate
    (CLI) and the two-turn degrade (async channels).

    Args:
        channel: Any communication channel object.

    Returns:
        ``True`` if ``channel`` advertises the synchronous confirmation
        primitive, ``False`` otherwise.
    """
    return callable(getattr(channel, "request_confirmation", None))


__all__ = [
    "IncomingMessage",
    "MessageHandler",
    "SupportsConfirmation",
    "supports_sync_confirmation",
]
