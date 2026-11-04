"""Map httpx failures inside a REST CMMS call to Machina connector errors.

The REST CMMS connectors call ``Response.raise_for_status()`` and
``Response.json()``, and httpx also *raises* on its own: a timeout, a refused
connection, a protocol error. :func:`rest_errors` turns all of these into the
:class:`~machina.exceptions.ConnectorError` family, so callers handle one
exception hierarchy whichever CMMS is configured, and no httpx message (which
can carry the request URL) reaches the agent or an MCP client.

A failed *write* needs more than a failure type: the caller must know whether
the CMMS may have applied it. httpx raises some errors before any byte of the
request is sent (the connection could not be opened, no pooled connection
became free, the URL scheme is unsupported); no request reached the CMMS.
A 4xx status is the CMMS refusing the request. Any other failure happens
after sending began, and the write may have been applied, so the message says
its outcome is unknown instead of reporting a failure that invites a
duplicating retry.

The vendor connectors (SAP PM, Maximo, UpKeep) check most statuses
themselves instead of calling ``raise_for_status()``; :func:`status_error`
builds the same error for them. Some of their writes send a read first (SAP
PM's CSRF token fetch, Maximo's work-order lookup), after which "no request
was sent" would be untrue, so every write of theirs says
:data:`WRITE_REQUEST_NOT_SENT` for a failure before the write request
(:func:`write_not_sent` for one in such a read), never :data:`WRITE_NOT_SENT`.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog

from machina.exceptions import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = structlog.get_logger(__name__)

#: How the message of every write whose outcome is unknown ends.
WRITE_OUTCOME_UNKNOWN = (
    "The write operation's outcome is unknown. It may have been applied. "
    "Check the CMMS before retrying."
)

#: How the message of every write that failed before its request was sent ends.
WRITE_NOT_SENT = "No request was sent to the CMMS."

#: How it ends instead for the vendor connectors, some of whose writes send a
#: read first (a token fetch, a lookup).
WRITE_REQUEST_NOT_SENT = "The write request was not sent to the CMMS."

_INVALID_URL = "invalid URL (check 'url' and the endpoint paths)"
_NOT_ASCII = "a request header is not ASCII (check the credentials)"
_NOT_UTF8 = "a request value cannot be encoded as UTF-8"


def write_outcome_unknown(
    vendor: str,
    operation: str,
    what_happened: str,
    *,
    error_type: type[ConnectorError] = ConnectorError,
) -> ConnectorError:
    """Build the error for a write the CMMS may have applied, and log it.

    Args:
        vendor: Label that opens the message, e.g. ``"CMMS"``.
        operation: The write, e.g. ``"create work order"``.
        what_happened: The rest of the first sentence, e.g. ``"was answered
            with HTTP 502"``. It must not carry the URL or a response body.
        error_type: The connector error to build, e.g.
            :class:`ConnectorTimeoutError` for a timeout.

    Returns:
        An error whose message ends with :data:`WRITE_OUTCOME_UNKNOWN`, for
        the caller to raise.
    """
    logger.warning("rest_write_outcome_unknown", connector=vendor, operation=operation)
    return error_type(f"{vendor} {operation} {what_happened}. {WRITE_OUTCOME_UNKNOWN}")


def status_error(
    vendor: str, operation: str, status: int, *, write: bool = False
) -> ConnectorError:
    """Build the error for an HTTP error status, as :func:`rest_errors` raises it.

    For a ``write``, a status other than 4xx (a 3xx, which redirects are not
    followed to, or a 5xx) leaves the outcome unknown
    (:func:`write_outcome_unknown`). Otherwise the message names the status
    (``CMMS read assets failed: HTTP 500``), as
    :class:`~machina.exceptions.ConnectorAuthError` for 401/403.

    Args:
        vendor: Label that opens the message, e.g. ``"Maximo"``.
        operation: The request, e.g. ``"create work order"``.
        status: The HTTP status code.
        write: Whether the request may have changed the CMMS.

    Returns:
        The error, for the caller to raise.
    """
    # Only a 4xx refuses a write. A 3xx can follow an applied write (303 See
    # Other is how POST-redirect-GET answers one) and a 5xx can come after the
    # CMMS committed it.
    if write and not 400 <= status < 500:
        return write_outcome_unknown(vendor, operation, f"was answered with HTTP {status}")
    message = f"{vendor} {operation} failed: HTTP {status}"
    if status in (401, 403):
        return ConnectorAuthError(message)
    return ConnectorError(message)


def write_not_sent(exc: ConnectorError) -> ConnectorError:
    """Restate a failure before a write request was sent, as the same error type.

    For a read that a write sends first, such as a token fetch or a lookup:
    the message gains :data:`WRITE_REQUEST_NOT_SENT`, so the caller knows the
    write cannot have been applied.

    Args:
        exc: The read's error. Its message must not carry the URL or a
            response body (every error :func:`rest_errors` raises qualifies).

    Returns:
        The restated error, for the caller to raise ``from exc``.
    """
    return type(exc)(f"{str(exc).rstrip('.')}. {WRITE_REQUEST_NOT_SENT}")


@contextmanager
def rest_errors(
    vendor: str,
    operation: str,
    *,
    write: bool = False,
    not_sent: str = WRITE_NOT_SENT,
) -> Iterator[None]:
    """Re-raise an httpx failure or an undecodable body as a connector error.

    For a read (the default):

    * an error status raised by ``Response.raise_for_status()`` becomes
      :class:`ConnectorAuthError` for 401/403 and :class:`ConnectorError`
      otherwise (``CMMS read assets failed: HTTP 500``);
    * a timeout becomes :class:`ConnectorTimeoutError`
      (``CMMS read assets timed out``);
    * any other httpx failure becomes :class:`ConnectorError`
      (``CMMS read assets failed: ReadError``);
    * a malformed URL raises ``httpx.InvalidURL``, which is not an
      ``httpx.HTTPError``, and becomes :class:`ConnectorConfigError`;
    * a header value httpx cannot encode as ASCII (a credential with an
      accented letter, say) becomes :class:`ConnectorConfigError`, and a
      body or query value it cannot encode as UTF-8 (an unpaired surrogate)
      a :class:`ConnectorError`, without the codec's message, which quotes
      the character, chained;
    * a body ``Response.json()`` cannot decode (an HTML login page from a
      proxy, say) becomes :class:`ConnectorError`
      (``CMMS read assets failed: response is not JSON``).
      ``Response.json()`` decodes the raw bytes and ignores the declared
      charset, so a body that is not UTF-8 fails as ``not UTF-8``.

    For a ``write`` the exception types are the same, and the message says
    what the caller may assume about the CMMS:

    * a failure before the request was sent (``ConnectError``,
      ``ConnectTimeout``, ``PoolTimeout``, ``UnsupportedProtocol``, a
      malformed URL, a value httpx cannot encode) reads as for a read and
      ends with ``not_sent``. This holds only if the block sends one
      request: a failure to open the connection for a second request says
      nothing about the first, so a ``write`` block must not wrap more than
      the write's own request (SAP PM's re-send after a CSRF challenge, and
      a retry after a 429, follow a request the CMMS refused unprocessed);
    * a 4xx status is the CMMS refusing the request, and reads as for a
      read (``CMMS create work order failed: HTTP 422``);
    * any other error status (a 3xx, which redirects are not followed to,
      or a 5xx), any other failure (it happens after sending began) and an
      undecodable 2xx body end with :data:`WRITE_OUTCOME_UNKNOWN`, and are
      logged as ``rest_write_outcome_unknown``.

    A message names the vendor, the operation and the status or failure
    type, never the URL (which httpx puts in its own messages) or the body;
    the original exception stays chained as ``__cause__``. Any other
    exception, connector errors and ``asyncio.CancelledError`` included,
    passes through unchanged.

    Wrap only the request, the status check and the decoding: map and parse
    records after the block, so that an error there (a plugin coercer's
    ``json.JSONDecodeError`` included) is not reported as a failed call.
    httpx is imported lazily, so the ``cmms-rest`` extra stays optional.

    Args:
        vendor: Label that opens the message, e.g. ``"CMMS"``.
        operation: What the wrapped code does, e.g. ``"create work order"``.
        write: Whether the wrapped request may change the CMMS.
        not_sent: The sentence a ``write`` that failed before its request
            was sent ends with: :data:`WRITE_REQUEST_NOT_SENT` where the
            operation sent a read first.

    Example:
        ```python
        with rest_errors("CMMS", "create work order", write=True):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                body = resp.json()
        work_order = parse_work_order(body)
        ```
    """
    import httpx

    # Raised before any byte of the request is sent. httpx raises these only
    # by mapping httpcore's, and httpcore raises ConnectError and
    # ConnectTimeout only while it opens the connection (connect_tcp,
    # connect_unix_socket, start_tls: the failures its own connection retries
    # cover), PoolTimeout while it waits for a pooled connection, and
    # UnsupportedProtocol before it picks one.
    before_sending = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        httpx.UnsupportedProtocol,
    )
    try:
        yield
    except httpx.HTTPStatusError as exc:
        raise status_error(vendor, operation, exc.response.status_code, write=write) from exc
    except httpx.HTTPError as exc:
        timeout = isinstance(exc, httpx.TimeoutException)
        error_type = ConnectorTimeoutError if timeout else ConnectorError
        kind = type(exc).__name__
        message = f"{vendor} {operation} " + ("timed out" if timeout else f"failed: {kind}")
        if not write:
            raise error_type(message) from exc
        if isinstance(exc, before_sending):
            raise error_type(f"{message}. {not_sent}") from exc
        when = "after sending began"
        failure = f"timed out {when}" if timeout else f"was interrupted {when}: {kind}"
        raise write_outcome_unknown(vendor, operation, failure, error_type=error_type) from exc
    except httpx.InvalidURL as exc:
        message = f"{vendor} {operation} failed: {_INVALID_URL}"
        raise ConnectorConfigError(f"{message}. {not_sent}" if write else message) from exc
    except UnicodeEncodeError as exc:
        # httpx encodes while it builds the request, so nothing was sent:
        # header values as ASCII, a JSON body and query params as UTF-8 (which
        # fails on an unpaired surrogate). The codec's message quotes the
        # character and its position in the value, which for a header is
        # usually a credential: drop it.
        problem = _NOT_ASCII if exc.encoding == "ascii" else _NOT_UTF8
        message = f"{vendor} {operation} failed: {problem}"
        error = ConnectorConfigError if exc.encoding == "ascii" else ConnectorError
        raise error(f"{message}. {not_sent}" if write else message) from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        problem = "not UTF-8" if isinstance(exc, UnicodeDecodeError) else "not JSON"
        if write:
            raise write_outcome_unknown(
                vendor,
                operation,
                f"was answered with a success status, but the response is {problem}",
            ) from exc
        raise ConnectorError(f"{vendor} {operation} failed: response is {problem}") from exc
