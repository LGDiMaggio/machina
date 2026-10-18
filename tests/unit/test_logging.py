"""Tests for the structured logging module."""

from __future__ import annotations

import ast
import io
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from machina.observability.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo ``configure_logging()``: root handlers, root level and structlog's config.

    ``configure_logging()`` turns on ``cache_logger_on_first_use``. Left on, it
    pins a module-level logger that a later test uses to whatever configuration
    is active at that moment.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield
    finally:
        structlog.reset_defaults()
        root.handlers[:] = handlers
        root.setLevel(level)


class TestConfigureLogging:
    """Test logging configuration."""

    def test_configure_default(self) -> None:
        configure_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1

    def test_configure_debug_level(self) -> None:
        configure_logging(level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_configure_json_output(self) -> None:
        configure_logging(json_output=True)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert formatter is not None

    def test_configure_replaces_handlers(self) -> None:
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler())
        root.addHandler(logging.StreamHandler())
        assert len(root.handlers) >= 2
        configure_logging()
        assert len(root.handlers) == 1

    def test_configure_case_insensitive_level(self) -> None:
        configure_logging(level="warning")
        root = logging.getLogger()
        assert root.level == logging.WARNING


class TestGetLogger:
    """Test logger creation with context binding."""

    def test_get_logger_returns_bound_logger(self) -> None:
        logger = get_logger("test")
        assert hasattr(logger, "info")  # structlog BoundLogger or LazyProxy

    def test_get_logger_with_context(self) -> None:
        logger = get_logger("test", connector="sap_pm", asset_id="P-201")
        # The bound context should be accessible
        assert logger is not None

    def test_get_logger_without_name(self) -> None:
        logger = get_logger()
        assert hasattr(logger, "info")  # structlog BoundLogger or LazyProxy

    def test_get_logger_without_context(self) -> None:
        logger = get_logger("test.module")
        assert hasattr(logger, "info")  # structlog BoundLogger or LazyProxy


# Values a REST connector's frame holds while an exception passes through it.
_SECRETS = (
    "Bearer s3cr3t-bearer-token",
    "s3cr3t-password",
    "s3cr3t-api-key",
    "s3cr3t-client-secret",
)


def _call_cmms() -> None:
    """Fail while this frame holds credentials, as ``_rest_read_work_orders`` does."""
    headers = {"Authorization": _SECRETS[0]}  # noqa: F841 — a frame local
    password = _SECRETS[1]  # noqa: F841
    api_key = _SECRETS[2]  # noqa: F841
    client_secret = _SECRETS[3]  # noqa: F841
    raise RuntimeError("CMMS returned 401")


def _map_response() -> None:
    """Re-raise the CMMS failure as a mapping error, from a frame holding a token."""
    token = _SECRETS[0]  # noqa: F841 — a frame local
    try:
        _call_cmms()
    except RuntimeError as exc:
        raise KeyError("wonum") from exc


def _log_structlog_failure(log: Any = None, fail: Callable[[], None] = _call_cmms) -> None:
    try:
        fail()
    except Exception:
        (log or structlog.get_logger("test.locals")).error("cmms_call_failed", exc_info=True)


def _log_stdlib_failure() -> None:
    """Log the way a third-party library does, through stdlib ``logging``."""
    try:
        _call_cmms()
    except RuntimeError:
        logging.getLogger("thirdparty.client").exception("request failed")


_SOURCES = {"structlog": _log_structlog_failure, "stdlib": _log_stdlib_failure}


def _configure(*, json_output: bool = False, renderer: Any = None) -> io.StringIO:
    """Run ``configure_logging()`` and return the stream its handler writes to.

    With *renderer*, the formatter's final renderer is swapped for it; every
    processor that runs before the renderer stays as configured.
    """
    configure_logging(json_output=json_output)
    handler = logging.getLogger().handlers[0]
    stream = io.StringIO()
    handler.setStream(stream)  # type: ignore[attr-defined]
    if renderer is not None:
        formatter: Any = handler.formatter
        formatter.processors = [*formatter.processors[:-1], renderer]
    return stream


def _assert_traceback_without_locals(output: str, *expected: str) -> None:
    assert "Traceback (most recent call last)" in output
    for text in expected:
        assert text in output
    for secret in _SECRETS:
        assert secret not in output


class TestTracebacksCarryNoFrameLocals:
    """A logged exception keeps its frames and message, never the frames' variables."""

    @pytest.mark.parametrize("source", sorted(_SOURCES))
    def test_console_renderer_never_receives_the_traceback_object(
        self, source: str, frame_locals_renderer: structlog.dev.ConsoleRenderer
    ) -> None:
        stream = _configure(renderer=frame_locals_renderer)

        _SOURCES[source]()

        _assert_traceback_without_locals(
            stream.getvalue(), "_call_cmms", "RuntimeError: CMMS returned 401"
        )

    @pytest.mark.parametrize("source", sorted(_SOURCES))
    def test_console_output_of_the_configured_renderer(self, source: str) -> None:
        """End to end, through the renderer ``configure_logging()`` builds (rich's
        traceback formatter shows locals by default when rich is installed)."""
        stream = _configure()

        _SOURCES[source]()

        _assert_traceback_without_locals(
            stream.getvalue(), "_call_cmms", "RuntimeError: CMMS returned 401"
        )

    @pytest.mark.parametrize("source", sorted(_SOURCES))
    def test_json_output_carries_the_traceback_as_a_string(self, source: str) -> None:
        stream = _configure(json_output=True)

        _SOURCES[source]()

        line = json.loads(stream.getvalue())
        assert "exc_info" not in line
        assert isinstance(line["exception"], str)
        _assert_traceback_without_locals(
            line["exception"], "_call_cmms", "RuntimeError: CMMS returned 401"
        )
        _assert_traceback_without_locals(stream.getvalue())

    def test_a_chained_exception_keeps_both_tracebacks(
        self, frame_locals_renderer: structlog.dev.ConsoleRenderer
    ) -> None:
        stream = _configure(renderer=frame_locals_renderer)

        _log_structlog_failure(fail=_map_response)

        output = stream.getvalue()
        _assert_traceback_without_locals(
            output,
            "_call_cmms",
            "RuntimeError: CMMS returned 401",
            "direct cause of the following exception",
            "_map_response",
            "KeyError: 'wonum'",
        )

    def test_a_handler_added_after_configure_logging_gets_the_text(
        self, frame_locals_renderer: structlog.dev.ConsoleRenderer
    ) -> None:
        """A handler an application adds itself formats records with its own
        processors; a structlog event reaches it with the traceback already
        rendered as text."""
        _configure()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    frame_locals_renderer,
                ]
            )
        )
        logging.getLogger().addHandler(handler)

        _log_structlog_failure()

        _assert_traceback_without_locals(
            stream.getvalue(), "_call_cmms", "RuntimeError: CMMS returned 401"
        )

    def test_a_logger_made_before_configure_logging_uses_its_handler(self) -> None:
        """A module-level ``get_logger(__name__, connector=...)`` runs at import,
        before the application calls ``configure_logging()``."""
        log = get_logger("test.early", connector="cmms_0")
        stream = _configure(json_output=True)

        _log_structlog_failure(log)

        line = json.loads(stream.getvalue())
        assert line["connector"] == "cmms_0"
        _assert_traceback_without_locals(line["exception"], "_call_cmms")


_SRC = Path(__file__).resolve().parents[2] / "src" / "machina"


def _is_traceback_text_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "traceback_text") or (
        isinstance(func, ast.Attribute) and func.attr == "traceback_text"
    )


# Keywords that switch frame locals on, and formatters that show them by default.
_LOCALS_KEYWORDS = {"show_locals", "capture_locals", "tracebacks_show_locals"}
_LOCALS_BY_DEFAULT = {"RichTracebackFormatter", "ExceptionDictTransformer"}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _locals_capable_calls(source: str) -> list[tuple[int, str]]:
    """Calls that can hand a renderer a live traceback, and with it frame locals.

    Machina logs ``exception=traceback_text(exc)`` instead: an application that
    never calls ``configure_logging()`` renders with structlog's defaults, which
    print every frame's locals for an ``exc_info`` when rich is installed.
    Keyword arguments forwarded through ``**kwargs`` are not seen.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name | ast.Attribute) and _name(node) == "dict_tracebacks":
            found.append((node.lineno, "dict_tracebacks"))
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "exc_info":
                found.append((node.lineno, "exc_info="))
            elif keyword.arg in _LOCALS_KEYWORDS and not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            ):
                found.append((node.lineno, f"{keyword.arg}="))
            elif keyword.arg == "exception" and not _is_traceback_text_call(keyword.value):
                found.append((node.lineno, "exception= not from traceback_text()"))
        func = node.func
        if _name(func) in _LOCALS_BY_DEFAULT and not any(
            keyword.arg == "show_locals" for keyword in node.keywords
        ):
            found.append((node.lineno, f"{_name(func)}() shows locals by default"))
        # ``Task.exception()`` takes no arguments; a logger's takes the event.
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"exception", "aexception"}
            and (node.args or node.keywords)
        ):
            found.append((node.lineno, f".{func.attr}(...)"))
    return found


class TestNoLocalsCapableLogCalls:
    """Machina's own log calls never pass a live exception to a renderer."""

    def test_machina_source_has_none(self) -> None:
        paths = sorted(_SRC.rglob("*.py"))
        # An empty scan would pass: make sure it read the package.
        assert _SRC / "observability" / "logging.py" in paths

        found = [
            f"{path.relative_to(_SRC.parent)}:{line}: {call}"
            for path in paths
            for line, call in _locals_capable_calls(path.read_text(encoding="utf-8"))
        ]

        assert found == []

    @pytest.mark.parametrize(
        "call",
        [
            'log.warning("e", exc_info=True)',
            "log.bind(exc_info=exc)",
            'logger.exception("e")',
            'logger.exception(f"e {x}")',
            "logger.aexception(event)",
            "RichTracebackFormatter(show_locals=True)",
            "structlog.dev.RichTracebackFormatter()",
            "ExceptionDictTransformer()",
            "processors = [structlog.processors.dict_tracebacks]",
            "RichHandler(rich_tracebacks=True, tracebacks_show_locals=True)",
            "TracebackException.from_exception(exc, capture_locals=flag)",
            'log.warning("e", exception=exc)',
        ],
    )
    def test_the_scan_flags(self, call: str) -> None:
        assert _locals_capable_calls(call)

    @pytest.mark.parametrize(
        "call",
        [
            "task.exception()",
            'log.warning("e", exception=traceback_text(exc))',
            'log.warning("e", exception=logging_module.traceback_text(exc))',
            "RichTracebackFormatter(show_locals=False)",
            "ExceptionDictTransformer(show_locals=False)",
            "RichHandler(rich_tracebacks=True, tracebacks_show_locals=False)",
        ],
    )
    def test_the_scan_ignores(self, call: str) -> None:
        assert _locals_capable_calls(call) == []


def test_secret_named_fields_are_redacted() -> None:
    stream = _configure(json_output=True)

    structlog.get_logger("test.redact").info("connecting", password="hunter2", api_key="k-123")

    line = json.loads(stream.getvalue())
    assert line["password"] == "***REDACTED***"
    assert line["api_key"] == "***REDACTED***"
    assert "hunter2" not in stream.getvalue()
