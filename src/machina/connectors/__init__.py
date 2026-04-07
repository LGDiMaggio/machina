"""Connector layer — integrations with external industrial systems."""

from machina.connectors.base import BaseConnector, ConnectorHealth, ConnectorRegistry
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel, TelegramConnector
from machina.connectors.docs.document_store import DocumentStoreConnector

# Short public API aliases (see CLAUDE.md naming conventions)
DocumentStore = DocumentStoreConnector
GenericCmms = GenericCmmsConnector
Telegram = TelegramConnector

__all__ = [
    "BaseConnector",
    "CliChannel",
    "ConnectorHealth",
    "ConnectorRegistry",
    "DocumentStore",
    "DocumentStoreConnector",
    "GenericCmms",
    "GenericCmmsConnector",
    "Telegram",
    "TelegramConnector",
]

