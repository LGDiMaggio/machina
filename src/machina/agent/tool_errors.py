"""What an exception may show outside the process, and the read-tool error result.

:func:`public_error_text` is the only text an exception shows at the model,
MCP and workflow boundaries: the agent's read tools, the MCP tools and
resources (:mod:`machina.mcp.errors`), workflow step errors and the agent's
``execute_workflow`` tool. :func:`error_diagnostics` keeps the detail in the
server log instead.

:func:`read_tool_error` is shared by every place a read-tool failure is caught
— the dispatch guard in :meth:`machina.agent.runtime.Agent._execute_tool` and
:func:`machina.agent.maintenance_schedule.get_maintenance_schedule` — so a
failed read is logged, scrubbed and shown to the model the same way everywhere.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from machina.agent.prompts import safe_text
from machina.exceptions import ConnectorUnsupportedFilterError, MachinaError
from machina.observability.logging import traceback_text

logger = structlog.get_logger(__name__)


def public_error_text(exc: BaseException) -> str:
    """Return the text *exc* may show to the model, an MCP client or a workflow result.

    A :class:`~machina.exceptions.MachinaError` message is written to be shown:
    it names what failed, never a URL, credential or backend value. It is
    scrubbed of user-home and UNC paths
    (:func:`~machina.agent.prompts.safe_text`), and an empty one is reported by
    its type name.

    Any other exception is a bug or an unmapped library error, and its message
    can carry a request URL, a host, a token or a backend value. Only its type
    is shown; the message and traceback go to the server log
    (:func:`error_diagnostics`). The wording says the error was unexpected, so
    it never reads as an outage of the backend.

    Args:
        exc: The exception to describe.

    Returns:
        The text to show. Never empty.
    """
    if isinstance(exc, MachinaError):
        return safe_text(str(exc) or type(exc).__name__)
    return f"Unexpected error ({type(exc).__name__}). The details are in the server log."


def error_diagnostics(exc: BaseException) -> dict[str, str]:
    """Return the log fields that keep *exc*'s detail server-side.

    For an exception that is not a :class:`~machina.exceptions.MachinaError`,
    its traceback (``exception``), as plain text
    (:func:`~machina.observability.logging.traceback_text` says why not
    ``exc_info``). For a ``MachinaError`` raised from a foreign exception
    (``raise ConnectorError(...) from exc``), the cause's type
    (``cause_type``) only: the REST helpers and the mappers chain httpx and
    pydantic errors whose text carries request URLs and record values, which
    their messages leave out so that the log does too. A ``MachinaError``
    with no foreign cause needs neither.

    Args:
        exc: The exception being logged.

    Returns:
        The fields to add to the log event, possibly none.
    """
    if not isinstance(exc, MachinaError):
        return {"exception": traceback_text(exc)}
    cause = exc.__cause__
    if cause is not None and not isinstance(cause, MachinaError):
        return {"cause_type": type(cause).__name__}
    return {}


def read_tool_error(exc: Exception, event: str, **context: Any) -> dict[str, str]:
    """Log a failed read and return the ``{"error": ...}`` result for the model.

    The read degrades instead of ending the turn: the model sees the error and
    can tell the user the data is unavailable. It sees only
    :func:`public_error_text`: a Machina error's message, scrubbed of
    user-home / UNC paths, or the type of any other exception.

    The failure is logged at WARNING, except a
    :class:`~machina.exceptions.ConnectorUnsupportedFilterError`: a connector
    refusing a filter it cannot apply is an expected outcome (it recurs on
    every such call), so it is logged at INFO, with the same fields.

    Args:
        exc: The exception the read raised.
        event: The log event name.
        **context: Structured log context (``connector``, ``asset_id``,
            ``operation``, ...).

    Returns:
        The tool result, ``{"error": <message>}``.

    Raises:
        asyncio.CancelledError: When the current task is being cancelled. A
            connector that turned its own cancellation into an ordinary
            exception (by catching ``BaseException``, or through a
            ``TaskGroup``'s ``ExceptionGroup``) must not have it degraded into
            a tool error, or the turn runs on past the caller's deadline.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError from exc
    # Machina's own errors (a ConnectorError, or a DomainValidationError for a
    # bad argument) already say what failed; one raised from a library error
    # adds the cause's type. Anything else is a bug or an unmapped library
    # error: keep its traceback, so the detail the model does not see is in
    # the log.
    context.update(error_diagnostics(exc))
    log = logger.info if isinstance(exc, ConnectorUnsupportedFilterError) else logger.warning
    log(event, **context, error_type=type(exc).__name__, error=str(exc))
    return {"error": public_error_text(exc)}
