"""What a failed MCP tool call or resource read shows the client.

FastMCP turns an exception a tool raises into an ``isError`` result whose text
is ``Error executing tool <name>: <message>``, and an exception a resource
template raises into a JSON-RPC error carrying its message. A connector or
library exception's message can carry a request URL, a host, a token or a
backend value, so the server registers every tool and the CMMS resource
templates wrapped by :func:`guard_tool` / :func:`guard_resource`: the client
sees only :func:`~machina.agent.tool_errors.public_error_text`, and the
detail goes to the server log.

The wrappers keep the wrapped function's name, docstring and signature
(``functools.wraps``), which FastMCP reads to build the tool's input and
output schemas, inject the request ``Context`` and match resource-template
parameters. Argument validation runs before the tool body, so its message,
which quotes the caller's own input, is unchanged.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

import structlog

from machina.agent.tool_errors import error_diagnostics, public_error_text
from machina.connectors.base import get_sandbox_mode
from machina.connectors.cmms._http_errors import WRITE_OUTCOME_UNKNOWN
from machina.exceptions import ConnectorUnsupportedFilterError, MachinaError

logger = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

#: Name of the attribute set to ``True`` on every guarded function, so a test
#: can check that no registered tool skipped the guard.
GUARDED_ATTRIBUTE = "__machina_guarded__"

#: Tools that can change an external system. When one raises an exception that
#: is not a Machina error, where it was raised (before or after the write went
#: out) is unknown, so the client is told the write may have been applied.
MCP_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "machina_create_work_order",
        "machina_update_work_order",
        "machina_close_work_order",
        "machina_cancel_work_order",
        "machina_send_message",
        "sap_pm_raw_iw38_notification",
        "maximo_raw_attribute_update",
    }
)

MESSAGE_MAY_HAVE_BEEN_SENT = "The message may have been sent."

# The sentence a write tool appends to an unexpected error; CMMS writes use
# the REST connectors' own wording.
_OUTCOME_UNKNOWN: dict[str, str] = {"machina_send_message": MESSAGE_MAY_HAVE_BEEN_SENT}


def _log_failure(event: str, exc: Exception, **context: str) -> None:
    # As read_tool_error logs a failed agent read: a connector refusing a
    # filter it cannot apply is expected, so it is logged at INFO.
    log = logger.info if isinstance(exc, ConnectorUnsupportedFilterError) else logger.warning
    log(event, **context, **error_diagnostics(exc), error_type=type(exc).__name__, error=str(exc))


def _client_text(exc: Exception, *, outcome_unknown: str | None = None) -> str:
    text = public_error_text(exc)
    # A Machina error says itself whether a write went out (the REST helpers'
    # "outcome is unknown" messages); any other exception cannot. In sandbox
    # mode no write goes out (every write path is @sandbox_aware), so the
    # sentence would be false. The tool's _runtime(ctx) set the mode in this
    # same context before it raised.
    if (
        outcome_unknown is not None
        and not isinstance(exc, MachinaError)
        and not get_sandbox_mode()
    ):
        return f"{text} {outcome_unknown}"
    return text


def guard_tool(fn: Callable[P, Awaitable[R]], *, write: bool = False) -> Callable[P, Awaitable[R]]:
    """Wrap an MCP tool so a failure shows the client only :func:`public_error_text`.

    The tool still fails: the client gets an ``isError`` result. A Machina
    error keeps its message, scrubbed of user-home and UNC paths; any other
    exception is reported by its type, and for a ``write`` tool with a
    sentence saying the write may have been applied. The failure is logged as
    ``mcp_tool_failed`` with the tool name, the error and, for a foreign
    exception or a Machina error raised from one, the traceback. Tool
    arguments are not logged. ``asyncio.CancelledError`` is not an
    ``Exception`` and passes through.

    Args:
        fn: The async tool function.
        write: Whether the tool can change an external system
            (:data:`MCP_WRITE_TOOLS`).

    Returns:
        The wrapped tool, with ``fn``'s name, docstring and signature.
    """
    name = fn.__name__
    outcome_unknown = _OUTCOME_UNKNOWN.get(name, WRITE_OUTCOME_UNKNOWN) if write else None

    @functools.wraps(fn)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            from mcp.server.fastmcp.exceptions import (  # type: ignore[import-not-found,unused-ignore]
                ToolError,
            )

            _log_failure("mcp_tool_failed", exc, tool=name)
            raise ToolError(_client_text(exc, outcome_unknown=outcome_unknown)) from None

    setattr(guarded, GUARDED_ATTRIBUTE, True)
    return guarded


def guard_resource(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Wrap an MCP resource template so a failure shows only :func:`public_error_text`.

    The read still fails, with a JSON-RPC error. It is logged as
    ``mcp_resource_failed`` with the resource function's name, like
    :func:`guard_tool`. Apply it under ``@server.resource(...)``.

    Args:
        fn: The async resource function.

    Returns:
        The wrapped function, with ``fn``'s name, docstring and signature.
    """
    name = fn.__name__

    @functools.wraps(fn)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            from mcp.server.fastmcp.exceptions import (  # type: ignore[import-not-found,unused-ignore]
                ResourceError,
            )

            _log_failure("mcp_resource_failed", exc, resource=name)
            raise ResourceError(public_error_text(exc)) from None

    setattr(guarded, GUARDED_ATTRIBUTE, True)
    return guarded


def returned_error(exc: Exception, *, tool: str) -> str:
    """Log an error a tool returns as an ``error`` entry, and return its public text.

    For the tools that answer a connector error with an ``{"error": ...}``
    entry instead of raising it: the client sees
    :func:`~machina.agent.tool_errors.public_error_text`, and the failure is
    logged as ``mcp_tool_failed`` as :func:`guard_tool` logs a raised one.

    Args:
        exc: The caught exception.
        tool: The tool name, for the log.

    Returns:
        The text for the ``error`` entry.
    """
    _log_failure("mcp_tool_failed", exc, tool=tool)
    return public_error_text(exc)


def is_guarded(fn: Any) -> bool:
    """Return whether *fn* was wrapped by :func:`guard_tool` or :func:`guard_resource`."""
    return getattr(fn, GUARDED_ATTRIBUTE, False) is True
