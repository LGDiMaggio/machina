"""Shared HTTP retry helper for CMMS REST connectors.

All Phase 2 CMMS connectors (SAP PM, Maximo, UpKeep) use a thin wrapper
around ``httpx.AsyncClient.request()`` that retries on:

* ``429 Too Many Requests`` — for every method.
* ``503 Service Unavailable`` — transient upstream failures, for idempotent
  methods only.
* ``httpx.TimeoutException``, ``httpx.ConnectError``, ``httpx.ReadError``
  — common transient network errors, for idempotent methods only.

Retries use exponential backoff capped at ``max_backoff``. A numeric
``Retry-After`` header on a retried response replaces the computed
backoff when it is at most ``max_backoff``; a longer one ends the
retries and the response is returned, because a retry inside the
server's window would most likely be refused again and waiting it out
would block the caller. No single wait exceeds ``max_backoff``, so one
call sleeps at most ``max_retries * max_backoff`` in total.

Non-retryable status codes (4xx other than 429, 5xx other than 503) are
returned to the caller unchanged so the connector layer can raise its
domain-specific exceptions. The final response after exhausting retries
is also returned, allowing the caller to still see the last
``status_code`` and headers.

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

# Methods that are idempotent by HTTP spec: retrying them after a network
# error or timeout is safe. POST/PATCH are NOT here — a timeout-after-success
# on a create would silently produce a duplicate, so they fail fast on network
# errors unless the caller explicitly opts in via ``retry_on_network_error``.
_IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


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
    retry_on_network_error: bool | None = None,
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
        max_backoff: Longest single wait, in seconds. The exponential
            backoff is capped at it, and a numeric ``Retry-After`` longer
            than it is not waited out: the response is returned instead.
        retry_on_network_error: Whether to retry on network/timeout errors.
            ``None`` (default) derives it from the method: idempotent methods
            (GET/HEAD/OPTIONS/PUT/DELETE) retry, non-idempotent ones
            (POST/PATCH) do not — because a timeout-after-success on a create
            would silently duplicate the resource. The same flag gates
            retrying a 503, which a gateway can return after the backend
            processed the request; a 429 is retried for every method, since
            the server refused it unprocessed.

    Returns:
        The final ``httpx.Response``. This is either the first success,
        the first non-retryable response, a retryable response whose
        ``Retry-After`` exceeds ``max_backoff``, or the final retry
        response after ``max_retries`` have been exhausted.

    Raises:
        httpx.TimeoutException, httpx.ConnectError, httpx.ReadError:
            Only re-raised when retries are exhausted.
    """
    import httpx

    if retry_on_network_error is None:
        retry_on_network_error = method.upper() in _IDEMPOTENT_METHODS

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
            if not retry_on_network_error or attempt >= max_retries:
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

        # 429 means the server rejected the request without processing it, so
        # retrying is safe for any method. A 503, however, can fire *after* a
        # non-idempotent request was already processed (e.g. a gateway timing
        # out post-success), so retrying a POST/PATCH on 503 risks a duplicate
        # — gate it on the same idempotency signal used for network errors.
        if resp.status_code == 503 and not retry_on_network_error:
            return resp

        retry_after = resp.headers.get("Retry-After", "").strip()
        # delay-seconds is ASCII digits; str.isdigit() alone also accepts
        # characters such as superscripts, which float() rejects.
        if retry_after.isascii() and retry_after.isdigit():
            backoff = float(retry_after)
            if backoff > max_backoff:
                # Retrying before the server's window ends would most likely
                # draw the same answer, and sleeping through it would block the
                # caller for that long: hand the response back so the
                # connector raises its error now.
                logger.warning(
                    "http_retry_after_exceeds_max_backoff",
                    retry_after=backoff,
                    max_backoff=max_backoff,
                    status_code=resp.status_code,
                    method=method,
                    url=url,
                )
                return resp
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
