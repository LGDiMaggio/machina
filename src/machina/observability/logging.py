"""Structured logging setup using structlog.

Every log message automatically includes ``connector``, ``asset_id``,
and ``operation`` fields when bound to the logger context.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import MutableMapping

_REDACT_PATTERNS = {"token", "password", "secret", "api_key", "client_secret", "authorization"}


def _redact_secrets(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact sensitive fields from log events."""
    for key in list(event_dict):
        if any(pattern in key.lower() for pattern in _REDACT_PATTERNS):
            event_dict[key] = "***REDACTED***"
    return event_dict


def traceback_text(exc: BaseException) -> str:
    """Format *exc*'s traceback as plain text, for an event's ``exception`` field.

    Machina logs ``exception=traceback_text(exc)`` instead of
    ``exc_info=True``: the text has the exception type, message, chained
    causes and each frame's file, line and function, but never the frames'
    local variables, whatever renderer the application configured.
    structlog's default console renderer prints those locals for an
    ``exc_info`` when rich is installed, and a REST connector's frames hold
    its auth headers.

    Args:
        exc: The exception to format.

    Returns:
        The traceback as ``format_exc_info`` renders it, without the
        trailing newline.
    """
    return "".join(traceback.format_exception(exc)).removesuffix("\n")


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure structured logging for Machina.

    A logged exception is rendered as a plain-text traceback, in the
    ``exception`` field: type, message and each frame's file, line and
    function, never the frames' local variables (a REST connector's frames
    hold its auth headers). This covers structlog events and records from
    third-party libraries that reach the root handler installed here. A
    handler an application adds later formats third-party records itself,
    so it needs its own ``format_exc_info`` to keep their locals out.

    The ``httpx`` and ``httpcore`` loggers are set to WARNING: below it they
    log each HTTP request with its full URL, and a CMMS request URL carries
    the CMMS host and the record IDs and filter values of the read. An
    application that wants those lines can lower the two loggers after this
    call.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, emit JSON lines; otherwise human-readable.
    """
    shared_processors: list[structlog.types.Processor] = [
        # Drop events below the level first, so ``format_exc_info`` below
        # never formats a traceback for an event that is then discarded.
        structlog.stdlib.filter_by_level,
        structlog.contextvars.merge_contextvars,
        _redact_secrets,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        # Render ``exc_info`` while the exception is current, so the event
        # carries text to every handler, including one added later; see below.
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # The exception reaches the renderer already rendered; ``plain_traceback``
        # keeps the renderer free of frame locals even if ``exc_info`` got here.
        renderer = structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Every record reaches the renderer with its traceback as plain text:
    # structlog's console renderer prints each frame's locals when rich is
    # installed, and JSON output would otherwise carry no traceback at all.
    # Running ``format_exc_info`` here as well covers third-party records,
    # which skip the chain above; on structlog events it finds nothing left.
    # Do not swap it for ``dict_tracebacks``, whose locals default is on, and
    # keep ``keep_exc_info`` off so the stdlib formatter adds no traceback.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    # Route to stderr when running as MCP stdio server to avoid
    # corrupting the JSON-RPC stream on stdout.
    stream = sys.stderr if os.environ.get("MACHINA_MCP_STDIO") else sys.stdout
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx logs every request at INFO (httpcore at DEBUG) with its full URL:
    # the CMMS host and the record IDs and filter values in the path and query.
    for library in ("httpx", "httpcore"):
        logging.getLogger(library).setLevel(logging.WARNING)


def get_logger(name: str | None = None, **initial_context: str) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger with optional initial context.

    Args:
        name: Logger name (typically ``__name__``).
        **initial_context: Key-value pairs bound to every log entry
            (e.g. ``connector="sap_pm"``, ``asset_id="P-201"``).

    Returns:
        A bound structlog logger. It takes its configuration on first use,
        so a module-level logger follows a later ``configure_logging()``.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name, **initial_context)
    return logger
