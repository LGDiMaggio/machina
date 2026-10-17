"""The result a read tool returns when its connector raises.

Shared by every place a read-tool failure is caught — the dispatch guard in
:meth:`machina.agent.runtime.Agent._execute_tool` and
:func:`machina.agent.maintenance_schedule.get_maintenance_schedule` — so a
failed read is logged, scrubbed and shown to the model the same way everywhere.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

import structlog

from machina.agent.prompts import safe_text
from machina.exceptions import MachinaError

logger = structlog.get_logger(__name__)


def read_tool_error(exc: Exception, event: str, **context: Any) -> dict[str, str]:
    """Log a failed read and return the ``{"error": ...}`` result for the model.

    The read degrades instead of ending the turn: the model sees the error and
    can tell the user the data is unavailable. The message is scrubbed of
    user-home / UNC paths (:func:`~machina.agent.prompts.safe_text`) before it
    reaches the LLM; an exception without a message (httpx timeouts often have
    none) is reported by its type name.

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
    # bad argument) already say what failed. Anything else is a bug or an
    # unmapped library error: keep its traceback, as plain text. Not as
    # ``exc_info``: structlog's console renderer prints each frame's local
    # variables, and a REST connector's frames hold its auth headers.
    if not isinstance(exc, MachinaError):
        context["traceback"] = "".join(traceback.format_exception(exc))
    logger.warning(event, **context, error_type=type(exc).__name__, error=str(exc))
    return {"error": safe_text(str(exc) or type(exc).__name__)}
