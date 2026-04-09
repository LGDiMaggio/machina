"""Pagination strategies for REST-based CMMS connectors.

Each strategy is an async generator that walks a paginated collection,
yielding one raw item dict at a time. The :data:`PaginationStrategy` type
alias is a discriminated union so configurations can round-trip through
YAML/JSON in the future.

Strategies cover the three common pagination patterns found in CMMS APIs:

* :class:`NoPagination` — single-shot request, response is a JSON list or a
  dict wrapping a list.
* :class:`OffsetLimitPagination` — ``?offset=X&limit=Y`` query params (UpKeep,
  Limble, and many REST APIs).
* :class:`PageNumberPagination` — ``?page=N&per_page=M`` query params
  (GitHub-style APIs).
* :class:`CursorPagination` — opaque cursor token from the response, followed
  until empty (Slack, modern cursor-based CMMS).

Each strategy supports an optional :attr:`items_path` (JMESPath expression)
for extracting the list of items from a wrapped response such as
``{"data": [...], "meta": {...}}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

import jmespath
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx


def _extract_items(data: Any, items_path: str | None) -> list[dict[str, Any]]:
    """Return the list of item dicts from a paginated response body.

    When ``items_path`` is provided, it is interpreted as a JMESPath
    expression against the full response body. When absent, the response
    body itself is expected to be a JSON list.
    """
    if items_path:
        result = jmespath.search(items_path, data)
        if result is None:
            return []
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


class NoPagination(BaseModel):
    """Fetch every item in a single request.

    Use this when the CMMS endpoint returns the entire collection in one
    response. This is the default and preserves the behaviour of earlier
    ``GenericCmmsConnector`` versions that did not support pagination.
    """

    type: Literal["none"] = "none"
    items_path: str | None = Field(
        default=None,
        description=(
            "Optional JMESPath expression to extract the item list from a "
            "wrapped response (e.g. 'data' for {'data': [...]}). When None, "
            "the response body itself is expected to be a JSON list."
        ),
    )

    async def iterate(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Perform a single GET and yield each item."""
        resp = await client.get(url, headers=headers, params=params or {})
        resp.raise_for_status()
        for item in _extract_items(resp.json(), self.items_path):
            yield item


class OffsetLimitPagination(BaseModel):
    """Paginate via ``?offset=X&limit=Y`` query parameters.

    Stops when a page returns fewer than ``page_size`` items (the typical
    end-of-collection signal for offset-style APIs).
    """

    type: Literal["offset_limit"] = "offset_limit"
    limit_param: str = "limit"
    offset_param: str = "offset"
    page_size: int = Field(default=100, gt=0)
    items_path: str | None = Field(
        default=None,
        description="Optional JMESPath expression to extract items per page.",
    )

    async def iterate(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk the collection by incrementing ``offset`` until a short page."""
        base_params: dict[str, str] = dict(params or {})
        offset = 0
        while True:
            req_params = {
                **base_params,
                self.limit_param: str(self.page_size),
                self.offset_param: str(offset),
            }
            resp = await client.get(url, headers=headers, params=req_params)
            resp.raise_for_status()
            items = _extract_items(resp.json(), self.items_path)
            if not items:
                return
            for item in items:
                yield item
            if len(items) < self.page_size:
                return
            offset += self.page_size


class PageNumberPagination(BaseModel):
    """Paginate via ``?page=N&per_page=M`` query parameters.

    Stops when a page returns fewer than ``page_size`` items. Supports APIs
    that number pages starting at either 0 or 1 via :attr:`start_page`.
    """

    type: Literal["page_number"] = "page_number"
    page_param: str = "page"
    size_param: str = "per_page"
    page_size: int = Field(default=100, gt=0)
    start_page: int = Field(default=1, ge=0)
    items_path: str | None = Field(
        default=None,
        description="Optional JMESPath expression to extract items per page.",
    )

    async def iterate(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk the collection by incrementing the page number."""
        base_params: dict[str, str] = dict(params or {})
        page = self.start_page
        while True:
            req_params = {
                **base_params,
                self.page_param: str(page),
                self.size_param: str(self.page_size),
            }
            resp = await client.get(url, headers=headers, params=req_params)
            resp.raise_for_status()
            items = _extract_items(resp.json(), self.items_path)
            if not items:
                return
            for item in items:
                yield item
            if len(items) < self.page_size:
                return
            page += 1


class CursorPagination(BaseModel):
    """Paginate via opaque cursor tokens returned in the response body.

    Each response is expected to contain a cursor value at
    :attr:`cursor_response_path` (JMESPath) that is sent as the
    :attr:`cursor_param` query parameter on the next request. Iteration
    stops when the cursor is missing, empty, or ``None``.
    """

    type: Literal["cursor"] = "cursor"
    cursor_param: str = "cursor"
    cursor_response_path: str = "next_cursor"
    items_path: str = Field(
        default="items",
        description="JMESPath expression to extract items per page (required).",
    )

    async def iterate(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk the collection by following cursor tokens."""
        base_params: dict[str, str] = dict(params or {})
        cursor: str | None = None
        while True:
            req_params = dict(base_params)
            if cursor:
                req_params[self.cursor_param] = cursor
            resp = await client.get(url, headers=headers, params=req_params)
            resp.raise_for_status()
            data = resp.json()
            for item in _extract_items(data, self.items_path):
                yield item
            next_cursor = jmespath.search(self.cursor_response_path, data)
            if not next_cursor:
                return
            cursor = str(next_cursor)


PaginationStrategy = Annotated[
    NoPagination | OffsetLimitPagination | PageNumberPagination | CursorPagination,
    Field(discriminator="type"),
]
"""Discriminated union of supported pagination strategies.

Use the concrete classes (``NoPagination``, ``OffsetLimitPagination``,
``PageNumberPagination``, ``CursorPagination``) to instantiate. The
discriminator ``type`` field enables deterministic deserialization from
external config.
"""
