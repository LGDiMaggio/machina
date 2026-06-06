"""Communication connector implementations."""

from machina.connectors.comms.email import EmailConnector
from machina.connectors.comms.slack import SlackConnector
from machina.connectors.comms.telegram import TelegramConnector
from machina.connectors.comms.types import (
    AFFIRMATION_TOKENS,
    DECLINE_TOKENS,
    IncomingMessage,
    MessageHandler,
    SupportsConfirmation,
    is_affirmation,
    is_decline,
    supports_sync_confirmation,
)

__all__ = [
    "AFFIRMATION_TOKENS",
    "DECLINE_TOKENS",
    "EmailConnector",
    "IncomingMessage",
    "MessageHandler",
    "SlackConnector",
    "SupportsConfirmation",
    "TelegramConnector",
    "is_affirmation",
    "is_decline",
    "supports_sync_confirmation",
]
