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


# Deterministic affirmation / decline token sets shared by every confirmation
# path (the agent runtime's two-turn degrade AND a channel's synchronous
# ``request_confirmation``). A weak local model must NOT be the component that
# interprets a "yes" — the parse here is mechanical, never delegated to the LLM.
# Tokens cover English + Italian; matching is case/whitespace-insensitive and
# requires the WHOLE message to be a single token (a compound like
# "ok, but set priority high" is deliberately neither — treated as unrelated).
# Unicode/homoglyph folding is intentionally deferred.
#
# Lives in the connectors layer (BELOW the agent layer) so both
# ``agent/runtime.py`` and the comms channels can import it without an upward
# dependency. Distinct from the data-coercion token sets in
# ``connectors/cmms/generic_coercers.py`` / ``excel.py`` (different domain).
AFFIRMATION_TOKENS: frozenset[str] = frozenset(
    {"y", "yes", "ok", "okay", "confirm", "confirmed", "sì", "si", "conferma", "procedi", "vai"}
)
DECLINE_TOKENS: frozenset[str] = frozenset({"n", "no", "annulla", "cancel", "stop", "abort"})


def is_affirmation(text: str) -> bool:
    """Deterministically recognise a bare affirmation (NOT via the LLM).

    Returns ``True`` only when the WHOLE message — after strip + lowercase — is
    a single recognised affirmation token (English or Italian). A compound
    message such as ``"ok, but set priority high"`` is NOT an affirmation, so a
    confirmation gate is never bypassed by an ambiguous "yes …" prefix.

    Args:
        text: The raw incoming message text.

    Returns:
        ``True`` if the message is exactly one affirmation token.
    """
    return text.strip().lower() in AFFIRMATION_TOKENS


def is_decline(text: str) -> bool:
    """Deterministically recognise a bare decline (NOT via the LLM).

    Mirror of :func:`is_affirmation` for decline tokens.

    Args:
        text: The raw incoming message text.

    Returns:
        ``True`` if the message is exactly one decline token.
    """
    return text.strip().lower() in DECLINE_TOKENS


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
    "AFFIRMATION_TOKENS",
    "DECLINE_TOKENS",
    "IncomingMessage",
    "MessageHandler",
    "SupportsConfirmation",
    "is_affirmation",
    "is_decline",
    "supports_sync_confirmation",
]
