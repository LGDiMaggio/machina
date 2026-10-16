"""Map httpx failures inside a REST CMMS call to Machina connector errors.

The REST CMMS connectors check response status codes themselves, but httpx
also *raises* — a timeout, a refused connection, a protocol error — and the
shared retry helper re-raises those once it gives up. :func:`rest_errors`
turns them into the :class:`~machina.exceptions.ConnectorError` family, so
callers handle one exception hierarchy whichever CMMS is configured.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from machina.exceptions import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def rest_errors(vendor: str, operation: str) -> Iterator[None]:
    """Re-raise an httpx failure inside a REST operation as a connector error.

    A timeout becomes :class:`ConnectorTimeoutError` and any other transport
    failure a :class:`ConnectorError`. An error status raised by
    ``Response.raise_for_status()`` becomes :class:`ConnectorAuthError` for
    401/403 and :class:`ConnectorError` otherwise. A malformed URL raises
    ``httpx.InvalidURL``, which is not an ``httpx.HTTPError``, and becomes
    :class:`ConnectorConfigError`. The message names the vendor, the operation
    and the status or failure type, but never the URL, which httpx puts in
    some of its own messages; the httpx exception stays chained as
    ``__cause__``. Any other exception, connector errors included, passes
    through unchanged. httpx is imported lazily, as the connectors do, so the
    ``cmms-rest`` extra stays optional.

    Args:
        vendor: Label that opens the message, e.g. ``"SAP PM"``.
        operation: What the wrapped code does, e.g. ``"create maintenance order"``.

    Example:
        ```python
        with rest_errors("Maximo", "create work order"):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(client, "POST", url, json=payload)
        ```
    """
    import httpx

    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = f"{vendor} {operation} failed: HTTP {status}"
        if status in (401, 403):
            raise ConnectorAuthError(message) from exc
        raise ConnectorError(message) from exc
    except httpx.TimeoutException as exc:
        raise ConnectorTimeoutError(f"{vendor} {operation} timed out") from exc
    except httpx.HTTPError as exc:
        raise ConnectorError(f"{vendor} {operation} failed: {type(exc).__name__}") from exc
    except httpx.InvalidURL as exc:
        raise ConnectorConfigError(
            f"{vendor} {operation} failed: invalid URL (check 'url' and the endpoint paths)"
        ) from exc
