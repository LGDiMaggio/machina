"""CMMS connector implementations.

Public API surface for the CMMS connector subsystem:

* :class:`GenericCmmsConnector` — configurable REST adapter for any CMMS,
  with pluggable auth and pagination strategies.
* Authentication strategies: :class:`BearerAuth`, :class:`BasicAuth`,
  :class:`ApiKeyHeaderAuth`, :class:`NoAuth`. Use
  :data:`AuthStrategy` as a type alias when annotating config code.
* Pagination strategies: :class:`NoPagination`,
  :class:`OffsetLimitPagination`, :class:`PageNumberPagination`,
  :class:`CursorPagination`. Use :data:`PaginationStrategy` as a type
  alias when annotating config code.
"""

from machina.connectors.cmms.auth import (
    ApiKeyHeaderAuth,
    AuthStrategy,
    BasicAuth,
    BearerAuth,
    NoAuth,
)
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.connectors.cmms.pagination import (
    CursorPagination,
    NoPagination,
    OffsetLimitPagination,
    PageNumberPagination,
    PaginationStrategy,
)

__all__ = [
    "ApiKeyHeaderAuth",
    "AuthStrategy",
    "BasicAuth",
    "BearerAuth",
    "CursorPagination",
    "GenericCmmsConnector",
    "NoAuth",
    "NoPagination",
    "OffsetLimitPagination",
    "PageNumberPagination",
    "PaginationStrategy",
]
