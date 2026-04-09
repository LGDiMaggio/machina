"""Connector layer — integrations with external industrial systems."""

from machina.connectors.base import BaseConnector, ConnectorHealth, ConnectorRegistry
from machina.connectors.cmms import (
    ApiKeyHeaderAuth,
    AuthStrategy,
    BasicAuth,
    BearerAuth,
    CursorPagination,
    GenericCmmsConnector,
    NoAuth,
    NoPagination,
    OffsetLimitPagination,
    PageNumberPagination,
    PaginationStrategy,
)
from machina.connectors.comms.telegram import CliChannel, TelegramConnector
from machina.connectors.docs.document_store import DocumentStoreConnector

# Short public API aliases (see CLAUDE.md naming conventions)
DocumentStore = DocumentStoreConnector
GenericCmms = GenericCmmsConnector
Telegram = TelegramConnector

__all__ = [
    "ApiKeyHeaderAuth",
    "AuthStrategy",
    "BaseConnector",
    "BasicAuth",
    "BearerAuth",
    "CliChannel",
    "ConnectorHealth",
    "ConnectorRegistry",
    "CursorPagination",
    "DocumentStore",
    "DocumentStoreConnector",
    "GenericCmms",
    "GenericCmmsConnector",
    "NoAuth",
    "NoPagination",
    "OffsetLimitPagination",
    "PageNumberPagination",
    "PaginationStrategy",
    "Telegram",
    "TelegramConnector",
]
