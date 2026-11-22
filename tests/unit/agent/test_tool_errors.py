"""Tests for the shared failed-read tool result and the boundary error text."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import httpx
import pydantic
import pytest
import structlog

from machina.agent.tool_errors import error_diagnostics, public_error_text, read_tool_error
from machina.exceptions import ConnectorError, ConnectorTimeoutError, DomainValidationError

# Values a foreign exception's message can carry, none of which may reach the
# model: an internal URL with a token, a bearer value, a user-home path, a UNC
# path, a vendor value, and SQL driver text naming the server and the user.
_URL = "http://internal-host:8443/api/plans?token=abc"
_NEEDLES = (
    "internal-host",
    "8443",
    "http://",
    "token=",
    "s3cr3t",
    "C:\\Users",
    "FILESRV01",
    "12.000",
    "db-internal",
    "svc_machina",
)


def _validation_error() -> pydantic.ValidationError:
    try:
        pydantic.TypeAdapter(int).validate_python("12.000 pcs")
    except pydantic.ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def _value_error() -> ValueError:
    try:
        int("12.000")
    except ValueError as exc:
        return exc
    raise AssertionError("expected a ValueError")


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", _URL)
    response = httpx.Response(503, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected an HTTPStatusError")


def _foreign_errors() -> list[BaseException]:
    return [
        RuntimeError(f"GET {_URL} failed"),
        httpx.ConnectError(f"All connection attempts failed: {_URL}"),
        _status_error(),
        _validation_error(),
        _value_error(),
        OSError(2, "No such file or directory", r"\\FILESRV01\manuals\pump.pdf"),
        OSError(r"cannot open C:\Users\ops\cmms\export.json"),
        KeyError("Authorization: Bearer s3cr3t-token"),
        ExceptionGroup("read failed", [RuntimeError(_URL)]),
        RuntimeError("(08001) server=db-internal.corp:1433;user=svc_machina login failed"),
    ]


def _foreign_ids() -> list[str]:
    return [
        "runtime",
        "connect",
        "http_status",
        "pydantic",
        "int_value",
        "unc_path",
        "home_path",
        "key",
        "group",
        "sql_driver",
    ]


@pytest.mark.parametrize("exc", _foreign_errors(), ids=_foreign_ids())
def test_a_foreign_exception_is_shown_by_its_type_only(exc: BaseException) -> None:
    text = public_error_text(exc)

    assert text == f"Unexpected error ({type(exc).__name__}). The details are in the server log."
    assert not [needle for needle in _NEEDLES if needle in text]
    # Never an outage report (B9-D4): a bug is not "the backend is down".
    for outage_word in ("unavailable", "unreachable", "timed out", "failed"):
        assert outage_word not in text.lower()


@pytest.mark.parametrize("exc", _foreign_errors(), ids=_foreign_ids())
@pytest.mark.asyncio
async def test_the_model_sees_no_foreign_text_and_the_log_keeps_it(exc: Exception) -> None:
    event = _logged(exc)
    error = read_tool_error(exc, "evt")

    assert error == {"error": public_error_text(exc)}
    assert event["error"] == str(exc)
    assert event["exception"]  # the traceback, which carries the message


def test_a_machina_error_keeps_its_message_scrubbed_of_paths() -> None:
    exc = ConnectorError(r"cannot read \\FILESRV01\cmms\export.json or C:\Users\ops\a.json")

    assert public_error_text(exc) == "cannot read export.json or a.json"


def test_a_machina_error_without_a_message_is_shown_by_its_type() -> None:
    assert public_error_text(ConnectorTimeoutError("")) == "ConnectorTimeoutError"


def test_diagnostics_name_the_foreign_cause_of_a_machina_error_by_type() -> None:
    """Only the cause's type: the REST helpers chain httpx errors whose text is
    the request URL, which neither their message nor the log carries."""
    request = httpx.Request("GET", _URL)
    try:
        try:
            httpx.Response(500, request=request).raise_for_status()
        except httpx.HTTPStatusError as cause:
            raise ConnectorError("CMMS read plans failed: HTTP 500") from cause
    except ConnectorError as exc:
        fields = error_diagnostics(exc)
        shown = public_error_text(exc)

    assert shown == "CMMS read plans failed: HTTP 500"
    assert fields == {"cause_type": "HTTPStatusError"}


def test_diagnostics_are_empty_for_a_machina_error_with_no_foreign_cause() -> None:
    assert error_diagnostics(ConnectorError("CMMS read assets failed: HTTP 500")) == {}
    try:
        try:
            raise ConnectorTimeoutError("inner")
        except ConnectorTimeoutError as inner:
            raise ConnectorError("outer") from inner
    except ConnectorError as exc:
        assert error_diagnostics(exc) == {}


def _logged(exc: Exception) -> dict[str, Any]:
    """Run ``read_tool_error`` on ``exc`` and return the one event it logged."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _capture(_logger: Any, method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        events.append((method, dict(event_dict)))
        return event_dict

    structlog.configure(processors=[_capture, structlog.processors.JSONRenderer()])
    try:
        read_tool_error(exc, "read_tool_failed", connector="cmms_0", asset_id="P-201")
    finally:
        structlog.reset_defaults()
    assert len(events) == 1
    method, event = events[0]
    assert method == "warning"
    return event


@pytest.mark.asyncio
async def test_the_model_sees_the_message_with_user_paths_scrubbed() -> None:
    error = read_tool_error(ConnectorError(r"cannot read C:\Users\ops\cmms\export.json"), "evt")

    assert error == {"error": "cannot read export.json"}


@pytest.mark.asyncio
async def test_an_empty_message_is_reported_by_type() -> None:
    # httpx timeouts often carry no message at all; a foreign error is shown
    # by its type anyway, and so is a Machina error without a message.
    assert read_tool_error(httpx.ReadTimeout(""), "evt") == {
        "error": "Unexpected error (ReadTimeout). The details are in the server log."
    }
    assert read_tool_error(ConnectorError(""), "evt") == {"error": "ConnectorError"}


@pytest.mark.asyncio
async def test_a_machina_error_raised_from_a_library_error_logs_the_cause_type() -> None:
    """A connector names only the library error's type, and so does the log."""
    try:
        try:
            raise OSError("server=db-internal.corp;user=svc_machina: login timeout")
        except OSError as cause:
            raise ConnectorError("SQL read failed (OSError)") from cause
    except ConnectorError as exc:
        event = _logged(exc)
        error = read_tool_error(exc, "evt")

    assert error == {"error": "SQL read failed (OSError)"}
    assert event["error"] == "SQL read failed (OSError)"
    assert event["cause_type"] == "OSError"
    assert "exception" not in event
    assert "db-internal" not in str(event)


@pytest.mark.asyncio
async def test_a_connector_error_is_logged_with_context_and_no_traceback() -> None:
    event = _logged(ConnectorTimeoutError("CMMS timed out"))

    assert event["event"] == "read_tool_failed"
    assert event["connector"] == "cmms_0"
    assert event["asset_id"] == "P-201"
    assert event["error_type"] == "ConnectorTimeoutError"
    assert event["error"] == "CMMS timed out"
    assert "exception" not in event
    assert "exc_info" not in event


@pytest.mark.asyncio
async def test_a_domain_validation_error_is_logged_without_traceback() -> None:
    """A refused argument is Machina's own, expected error, not a bug."""
    event = _logged(DomainValidationError("asset_id contains a wildcard"))

    assert event["error_type"] == "DomainValidationError"
    assert "exception" not in event


def _map_work_order(payload: dict[str, Any]) -> None:
    """A mapper that fails while its caller's frame holds credentials."""
    headers = {"Authorization": "Bearer s3cr3t-token"}  # noqa: F841 — a frame local
    payload["wonum"]


def _raised() -> Exception:
    try:
        _map_work_order({})
    except KeyError as exc:
        return exc
    raise AssertionError("expected KeyError")


@pytest.mark.asyncio
async def test_any_other_exception_keeps_its_traceback() -> None:
    """A mapper bug or an unmapped library error must stay debuggable."""
    event = _logged(_raised())

    assert event["error_type"] == "KeyError"
    assert "_map_work_order" in event["exception"]
    assert event["exception"].endswith("KeyError: 'wonum'")
    assert "exc_info" not in event


@pytest.mark.asyncio
async def test_a_logged_traceback_carries_no_frame_locals(
    frame_locals_renderer: structlog.dev.ConsoleRenderer,
) -> None:
    """structlog's console renderer prints the locals of an ``exc_info`` traceback.

    The traceback is logged as plain text instead, so an auth header held in a
    connector's frame never reaches the log.
    """
    out = io.StringIO()
    structlog.configure(
        processors=[frame_locals_renderer],
        logger_factory=structlog.PrintLoggerFactory(file=out),
    )
    try:
        read_tool_error(_raised(), "read_tool_failed", connector="cmms_0")
    finally:
        structlog.reset_defaults()

    assert "_map_work_order" in out.getvalue()
    assert "s3cr3t-token" not in out.getvalue()


@pytest.mark.asyncio
async def test_a_cancellation_in_progress_is_raised_not_reported() -> None:
    """An exception raised while the task is being cancelled is that cancellation
    in disguise (a library converted it): it propagates as one, so the caller's
    deadline still fires."""

    async def read() -> dict[str, str]:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return read_tool_error(ConnectorError("request aborted"), "evt")
        return {}

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await read()
