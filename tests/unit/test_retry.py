"""Unit tests for the shared CMMS retry helper.

Verifies retry behaviour on 429/503 responses and transient network
errors. ``asyncio.sleep`` is monkey-patched to a no-op so tests run
fast.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from machina.connectors.cmms.retry import request_with_retry


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace asyncio.sleep with a no-op so retry tests are instantaneous."""

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _fake_sleep)


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _SequenceClient:
    """Fake httpx.AsyncClient that yields responses/exceptions in order."""

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.calls: int = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        if not self._events:
            raise AssertionError("SequenceClient ran out of events")
        event = self._events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_returns_first_success_without_retrying() -> None:
    client = _SequenceClient([_FakeResponse(200)])
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 200
    assert client.calls == 1


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds() -> None:
    client = _SequenceClient(
        [
            _FakeResponse(503),
            _FakeResponse(503),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(
        client, "GET", "https://example.com/x", max_retries=3, base_backoff=0.01
    )
    assert resp.status_code == 200
    assert client.calls == 3


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds() -> None:
    client = _SequenceClient(
        [
            _FakeResponse(429, headers={"Retry-After": "1"}),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(
        client, "GET", "https://example.com/x", max_retries=3, base_backoff=0.01
    )
    assert resp.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_honours_numeric_retry_after_header() -> None:
    """A numeric Retry-After is parsed and used verbatim (no exponential)."""
    client = _SequenceClient(
        [
            _FakeResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse(200),
        ]
    )
    # The fake sleep swallows the value, but we still verify the call sequence works.
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_non_numeric_retry_after_falls_back_to_exponential() -> None:
    """Retry-After as an HTTP-date must not crash the helper."""
    client = _SequenceClient(
        [
            _FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_returns_last_response_when_retries_exhausted() -> None:
    """After max_retries we return the final response to the caller."""
    client = _SequenceClient([_FakeResponse(503), _FakeResponse(503)])
    resp = await request_with_retry(
        client, "GET", "https://example.com/x", max_retries=1, base_backoff=0.01
    )
    assert resp.status_code == 503
    assert client.calls == 2  # initial + 1 retry


@pytest.mark.asyncio
async def test_non_retryable_status_is_returned_immediately() -> None:
    client = _SequenceClient([_FakeResponse(401)])
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 401
    assert client.calls == 1


@pytest.mark.asyncio
async def test_retries_on_timeout_exception() -> None:
    client = _SequenceClient(
        [
            httpx.ConnectError("boom"),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(
        client, "GET", "https://example.com/x", max_retries=2, base_backoff=0.01
    )
    assert resp.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_timeout_raises_when_retries_exhausted() -> None:
    client = _SequenceClient(
        [
            httpx.ReadError("boom1"),
            httpx.ReadError("boom2"),
        ]
    )
    with pytest.raises(httpx.ReadError):
        await request_with_retry(
            client, "GET", "https://example.com/x", max_retries=1, base_backoff=0.01
        )
    assert client.calls == 2


@pytest.mark.asyncio
async def test_max_retries_zero_disables_retry() -> None:
    client = _SequenceClient([_FakeResponse(503)])
    resp = await request_with_retry(client, "GET", "https://example.com/x", max_retries=0)
    assert resp.status_code == 503
    assert client.calls == 1


@pytest.mark.asyncio
async def test_content_kwarg_is_forwarded() -> None:
    """The raw `content=` parameter must be forwarded to the underlying client."""

    class _CapturingClient:
        def __init__(self) -> None:
            self.calls: int = 0
            self.last_kwargs: dict[str, Any] = {}

        async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
            self.calls += 1
            self.last_kwargs = kwargs
            return _FakeResponse(200)

    client = _CapturingClient()
    await request_with_retry(client, "POST", "https://example.com/x", content=b"raw-body")
    assert client.calls == 1
    assert client.last_kwargs["content"] == b"raw-body"
