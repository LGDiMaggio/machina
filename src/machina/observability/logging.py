"""Structured logging setup using structlog.

Every log message automatically includes ``connector``, ``asset_id``,
and ``operation`` fields when bound to the logger context.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_REDACT_PATTERNS = {"token", "password", "secret", "api_key", "client_secret", "authorization"}


def _redact_secrets(logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive fields from log events."""
    for key in list(event_dict):
        if any(pattern in key.lower() for pattern in _REDACT_PATTERNS):
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure structured logging for Machina.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, emit JSON lines; otherwise human-readable.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _redact_secrets,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str | None = None, **initial_context: str) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger with optional initial context.

    Args:
        name: Logger name (typically ``__name__``).
        **initial_context: Key-value pairs bound to every log entry
            (e.g. ``connector="sap_pm"``, ``asset_id="P-201"``).

    Returns:
        A bound structlog logger.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger
