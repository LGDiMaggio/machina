"""Communication connector implementations."""

from machina.connectors.comms.email import EmailConnector
from machina.connectors.comms.slack import SlackConnector
from machina.connectors.comms.telegram import TelegramConnector
from machina.connectors.comms.types import IncomingMessage, MessageHandler

__all__ = [
    "EmailConnector",
    "IncomingMessage",
    "MessageHandler",
    "SlackConnector",
    "TelegramConnector",
]
