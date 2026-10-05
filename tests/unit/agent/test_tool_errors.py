"""Tests for the shared failed-read tool result."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import structlog

from machina.agent.tool_errors import read_tool_error
from machina.exceptions import ConnectorError, ConnectorTimeoutError


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
    # httpx timeouts often carry no message at all.
    assert read_tool_error(httpx.ReadTimeout(""), "evt") == {"error": "ReadTimeout"}


@pytest.mark.asyncio
async def test_a_connector_error_is_logged_with_context_and_no_traceback() -> None:
    event = _logged(ConnectorTimeoutError("CMMS timed out"))

    assert event["event"] == "read_tool_failed"
    assert event["connector"] == "cmms_0"
    assert event["asset_id"] == "P-201"
    assert event["error_type"] == "ConnectorTimeoutError"
    assert event["error"] == "CMMS timed out"
    assert event["exc_info"] is False


@pytest.mark.asyncio
async def test_any_other_exception_keeps_its_traceback() -> None:
    """A mapper bug or an unmapped library error must stay debuggable."""
    exc = KeyError("wonum")

    event = _logged(exc)

    assert event["error_type"] == "KeyError"
    assert event["exc_info"] is exc


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
