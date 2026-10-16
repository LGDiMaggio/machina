"""Unit tests for the shared CMMS retry helper.

Verifies retry behaviour on 429/503 responses and transient network
errors. ``asyncio.sleep`` is monkey-patched to record its delays instead
of sleeping, so tests run fast and can assert how long a call would wait.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from machina.connectors.cmms.retry import request_with_retry


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep with a recorder so retry tests are instantaneous.

    Request the fixture by name to read the delays the helper asked for.
    """
    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _fake_sleep)
    return recorded


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
async def test_honours_numeric_retry_after_header(sleeps: list[float]) -> None:
    """A numeric Retry-After within max_backoff is used verbatim (no exponential)."""
    client = _SequenceClient(
        [
            _FakeResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 200
    assert client.calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "max_backoff"),
    [
        pytest.param("8", 8.0, id="equal-to-default-max-backoff"),
        pytest.param("20", 30.0, id="within-raised-max-backoff"),
    ],
)
async def test_retry_after_up_to_max_backoff_is_honoured(
    retry_after: str, max_backoff: float, sleeps: list[float]
) -> None:
    """A Retry-After equal to max_backoff is still waited out, and a caller
    that raises max_backoff can honour a longer one."""
    client = _SequenceClient(
        [_FakeResponse(429, headers={"Retry-After": retry_after}), _FakeResponse(200)]
    )
    resp = await request_with_retry(
        client, "GET", "https://example.com/x", max_backoff=max_backoff
    )
    assert resp.status_code == 200
    assert sleeps == [float(retry_after)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "status_code"),
    [("GET", 429), ("GET", 503), ("POST", 429)],
)
@pytest.mark.parametrize("retry_after", ["9", "300"])
async def test_retry_after_beyond_max_backoff_returns_response_without_waiting(
    method: str, status_code: int, retry_after: str, sleeps: list[float]
) -> None:
    """A server asking for a longer pause than max_backoff would most likely
    answer an earlier retry the same way, and waiting it out would block the
    call (Retry-After: 300 over three retries is 15 minutes). The response is
    returned at once, so the connector raises its ConnectorError, and a write
    is sent exactly once."""
    refusal = _FakeResponse(status_code, headers={"Retry-After": retry_after})
    client = _SequenceClient([refusal] * 4)
    resp = await request_with_retry(
        client, method, "https://example.com/x", max_retries=3, max_backoff=8.0
    )
    assert resp.status_code == status_code
    assert client.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_retry_after_beyond_max_backoff_after_an_earlier_retry(
    sleeps: list[float],
) -> None:
    """The limit applies to every retried response, not just the first."""
    client = _SequenceClient(
        [
            _FakeResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse(503, headers={"Retry-After": "300"}),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 503
    assert client.calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param("Wed, 21 Oct 2026 07:28:00 GMT", id="http-date"),
        # str.isdigit() accepts a superscript two, but float() rejects it.
        pytest.param("\u00b2", id="non-ascii-digit"),
    ],
)
async def test_non_numeric_retry_after_falls_back_to_exponential(
    retry_after: str, sleeps: list[float]
) -> None:
    """A Retry-After that is not delay-seconds (ASCII digits only) must not
    crash the helper: it falls back to the exponential backoff."""
    client = _SequenceClient(
        [
            _FakeResponse(429, headers={"Retry-After": retry_after}),
            _FakeResponse(200),
        ]
    )
    resp = await request_with_retry(client, "GET", "https://example.com/x")
    assert resp.status_code == 200
    assert sleeps == [0.5]


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
async def test_post_not_retried_on_network_error() -> None:
    """A POST that fails with a network/timeout error must NOT be retried — the
    request may have reached the server (timeout-after-success), so retrying
    would create a duplicate work order."""
    client = _SequenceClient(
        [
            httpx.TimeoutException("boom"),
            _FakeResponse(200),
        ]
    )
    with pytest.raises(httpx.TimeoutException):
        await request_with_retry(
            client, "POST", "https://example.com/work_orders", max_retries=3, base_backoff=0.01
        )
    assert client.calls == 1  # no retry — failed fast


@pytest.mark.asyncio
async def test_post_not_retried_on_503() -> None:
    """503 can fire after a POST was already processed (gateway timeout
    post-success), so a non-idempotent POST is NOT retried on 503."""
    client = _SequenceClient([_FakeResponse(503), _FakeResponse(200)])
    resp = await request_with_retry(
        client, "POST", "https://example.com/work_orders", max_retries=3, base_backoff=0.01
    )
    assert resp.status_code == 503
    assert client.calls == 1


@pytest.mark.asyncio
async def test_get_retried_on_503() -> None:
    """503 on an idempotent GET is still retried."""
    client = _SequenceClient([_FakeResponse(503), _FakeResponse(200)])
    resp = await request_with_retry(
        client, "GET", "https://example.com/x", max_retries=3, base_backoff=0.01
    )
    assert resp.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_post_retried_on_429() -> None:
    """429 means the server rejected without processing, so retrying a POST is
    safe even though POST is non-idempotent."""
    client = _SequenceClient(
        [_FakeResponse(429, headers={"Retry-After": "1"}), _FakeResponse(201)]
    )
    resp = await request_with_retry(
        client, "POST", "https://example.com/work_orders", max_retries=3, base_backoff=0.01
    )
    assert resp.status_code == 201
    assert client.calls == 2


@pytest.mark.asyncio
async def test_patch_not_retried_on_network_error() -> None:
    """PATCH is non-idempotent by spec, so it fast-fails on network errors."""
    client = _SequenceClient([httpx.TimeoutException("boom"), _FakeResponse(200)])
    with pytest.raises(httpx.TimeoutException):
        await request_with_retry(
            client, "PATCH", "https://example.com/x", max_retries=3, base_backoff=0.01
        )
    assert client.calls == 1


@pytest.mark.asyncio
async def test_network_retry_explicit_opt_out_on_get() -> None:
    """retry_on_network_error=False forces fast-fail even for an idempotent GET."""
    client = _SequenceClient([httpx.ConnectError("boom"), _FakeResponse(200)])
    with pytest.raises(httpx.ConnectError):
        await request_with_retry(
            client,
            "GET",
            "https://example.com/x",
            max_retries=3,
            base_backoff=0.01,
            retry_on_network_error=False,
        )
    assert client.calls == 1


@pytest.mark.asyncio
async def test_put_retried_on_network_error() -> None:
    """PUT is idempotent by spec, so retrying it on a network error is safe."""
    client = _SequenceClient([httpx.ConnectError("boom"), _FakeResponse(200)])
    resp = await request_with_retry(
        client, "PUT", "https://example.com/x", max_retries=2, base_backoff=0.01
    )
    assert resp.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_post_network_retry_opt_in() -> None:
    """A caller can explicitly opt a POST back into network-error retries."""
    client = _SequenceClient([httpx.ConnectError("boom"), _FakeResponse(200)])
    resp = await request_with_retry(
        client,
        "POST",
        "https://example.com/x",
        max_retries=2,
        base_backoff=0.01,
        retry_on_network_error=True,
    )
    assert resp.status_code == 200
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
