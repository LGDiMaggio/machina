"""Connector layer — integrations with external industrial systems."""

from machina.connectors.base import BaseConnector, ConnectorHealth, ConnectorRegistry
from machina.connectors.cmms import (
    ApiKeyHeaderAuth,
    AuthStrategy,
    BasicAuth,
    BearerAuth,
    CursorPagination,
    GenericCmmsConnector,
    MaximoConnector,
    NoAuth,
    NoPagination,
    OAuth2ClientCredentials,
    OffsetLimitPagination,
    PageNumberPagination,
    PaginationStrategy,
    SapPmConnector,
    UpKeepConnector,
)
from machina.connectors.comms.telegram import CliChannel, TelegramConnector
from machina.connectors.docs.document_store import DocumentStoreConnector

# Short public API aliases (see CLAUDE.md naming conventions)
DocumentStore = DocumentStoreConnector
GenericCmms = GenericCmmsConnector
Maximo = MaximoConnector
SapPM = SapPmConnector
Telegram = TelegramConnector
UpKeep = UpKeepConnector

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
    "Maximo",
    "MaximoConnector",
    "NoAuth",
    "NoPagination",
    "OAuth2ClientCredentials",
    "OffsetLimitPagination",
    "PageNumberPagination",
    "PaginationStrategy",
    "SapPM",
    "SapPmConnector",
    "Telegram",
    "TelegramConnector",
    "UpKeep",
    "UpKeepConnector",
]
