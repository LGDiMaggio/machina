"""Shared HTTP retry helper for CMMS REST connectors.

All Phase 2 CMMS connectors (SAP PM, Maximo, UpKeep) use a thin wrapper
around ``httpx.AsyncClient.request()`` that retries on:

* ``429 Too Many Requests`` — honouring the ``Retry-After`` header when
  present (numeric seconds only).
* ``503 Service Unavailable`` — transient upstream failures.
* ``httpx.TimeoutException``, ``httpx.ConnectError``, ``httpx.ReadError``
  — common transient network errors.

Retries use exponential backoff with a cap. Non-retryable status codes
(4xx other than 429, 5xx other than 503) are returned to the caller
unchanged so the connector layer can raise its domain-specific
exceptions. The final response after exhausting retries is also
returned, allowing the caller to still see the last ``status_code`` and
headers.

Example:
    ```python
    import httpx

    from machina.connectors.cmms.retry import request_with_retry

    async with httpx.AsyncClient() as client:
        resp = await request_with_retry(
            client,
            "GET",
            "https://cmms.example.com/api/v2/assets",
            headers={"Authorization": "Bearer ..."},
            params={"limit": "100"},
        )
    ```
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BASE_BACKOFF: float = 0.5
DEFAULT_MAX_BACKOFF: float = 8.0

_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 503})


async def request_with_retry(
    client: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json: Any = None,
    content: Any = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_backoff: float = DEFAULT_BASE_BACKOFF,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
) -> Any:
    """Perform an HTTP request with retries on 429/503 and transient errors.

    Args:
        client: An ``httpx.AsyncClient`` instance.
        method: HTTP method — ``"GET"``, ``"POST"``, etc.
        url: Target URL.
        headers: Optional request headers.
        params: Optional query-string parameters.
        json: Optional JSON body (forwarded as ``json=`` to httpx).
        content: Optional raw body (forwarded as ``content=`` to httpx).
        max_retries: Maximum number of retry attempts after the initial
            request. ``0`` disables retries.
        base_backoff: Initial exponential-backoff delay in seconds.
        max_backoff: Cap on backoff delay in seconds.

    Returns:
        The final ``httpx.Response``. This is either the first success,
        the first non-retryable response, or the final retry response
        after ``max_retries`` have been exhausted.

    Raises:
        httpx.TimeoutException, httpx.ConnectError, httpx.ReadError:
            Only re-raised when retries are exhausted.
    """
    import httpx

    transient_exceptions: tuple[type[BaseException], ...] = (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.ReadError,
    )

    request_kwargs: dict[str, Any] = {}
    if headers is not None:
        request_kwargs["headers"] = headers
    if params is not None:
        request_kwargs["params"] = params
    if json is not None:
        request_kwargs["json"] = json
    if content is not None:
        request_kwargs["content"] = content

    attempt = 0
    while True:
        try:
            resp = await client.request(method, url, **request_kwargs)
        except transient_exceptions as exc:
            if attempt >= max_retries:
                raise
            backoff = min(base_backoff * (2**attempt), max_backoff)
            logger.warning(
                "http_network_retry",
                attempt=attempt + 1,
                max_retries=max_retries,
                backoff=backoff,
                method=method,
                url=url,
                error=str(exc),
            )
            await asyncio.sleep(backoff)
            attempt += 1
            continue

        if resp.status_code not in _RETRYABLE_STATUS or attempt >= max_retries:
            return resp

        retry_after = resp.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            backoff = float(retry_after)
        else:
            backoff = min(base_backoff * (2**attempt), max_backoff)
        logger.warning(
            "http_rate_limit_retry",
            attempt=attempt + 1,
            max_retries=max_retries,
            backoff=backoff,
            status_code=resp.status_code,
            method=method,
            url=url,
        )
        await asyncio.sleep(backoff)
        attempt += 1
