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


_PART_URL = "https://cmms.example.com/api/v2/parts/SECRET-PART"
_REDACTED = "/api/v2/parts/<listed part>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "event_name"),
    [
        ([httpx.ConnectError("refused"), _FakeResponse(200)], "http_network_retry"),
        ([_FakeResponse(429), _FakeResponse(200)], "http_rate_limit_retry"),
        (
            [_FakeResponse(429, headers={"Retry-After": "300"})],
            "http_retry_after_exceeds_max_backoff",
        ),
    ],
    ids=["network-retry", "rate-limit-retry", "retry-after-too-long"],
)
@pytest.mark.parametrize(
    ("log_url", "logged"), [(_REDACTED, _REDACTED), (None, _PART_URL)], ids=["log-url", "default"]
)
async def test_every_retry_warning_logs_log_url_in_place_of_the_url(
    events: list[Any], event_name: str, log_url: str | None, logged: str
) -> None:
    """A caller whose URL carries a record ID keeps it out of all three warnings."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        await request_with_retry(
            _SequenceClient(events), "GET", _PART_URL, max_backoff=8.0, log_url=log_url
        )

    (warning,) = [e for e in logs if e["event"] == event_name]
    assert warning["url"] == logged
    if log_url is not None:
        assert "SECRET-PART" not in repr(logs)


# One event sequence per retry warning: each logs the URL once.
_WARNINGS = [
    pytest.param(
        [httpx.ConnectError("refused"), _FakeResponse(200)], "http_network_retry", id="network"
    ),
    pytest.param([_FakeResponse(429), _FakeResponse(200)], "http_rate_limit_retry", id="429"),
    pytest.param(
        [_FakeResponse(429, headers={"Retry-After": "300"})],
        "http_retry_after_exceeds_max_backoff",
        id="retry-after",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "event_name"), _WARNINGS)
@pytest.mark.parametrize(
    ("url", "logged", "secrets"),
    [
        pytest.param(
            "https://maximo.example.com/maximo/oslc/os/mxwo?pageno=2&oslc.pageSize=100"
            "&oslc.where=assetnum%3D%22P-201%22&_lid=svc&_lpwd=pw123",
            "https://maximo.example.com/maximo/oslc/os/mxwo",
            ("P-201", "pageno", "_lpwd", "pw123", "oslc.where"),
            id="maximo-next-page",
        ),
        pytest.param(
            "https://sap.example.com:44300/sap/opu/odata/sap/API_EQUIPMENT/Equipment"
            "?$skiptoken=100&$filter=Equipment%20eq%20'10000001'",
            "https://sap.example.com:44300/sap/opu/odata/sap/API_EQUIPMENT/Equipment",
            ("10000001", "skiptoken", "$filter"),
            id="sap-next",
        ),
        pytest.param(
            "https://sap.example.com/sap/opu/odata/sap/API_MAINTENANCEORDER/"
            "MaintenanceOrder('4000123')",
            "https://sap.example.com/sap/opu/odata/sap/API_MAINTENANCEORDER/"
            "MaintenanceOrder(<key>)",
            ("4000123",),
            id="odata-key",
        ),
        pytest.param(
            "https://cmms.example.com/odata/Orders(Key1='A-77',Key2=5)/Items?$top=1",
            "https://cmms.example.com/odata/Orders(<key>)/Items",
            ("A-77", "Key2", "$top"),
            id="odata-composite-key",
        ),
        pytest.param(
            "https://svc_machina:s3cr3t@cmms.example.com:8443/api/v2/users?pagesize=1#frag",
            "https://cmms.example.com:8443/api/v2/users",
            ("svc_machina", "s3cr3t", "pagesize", "frag"),
            id="user-info",
        ),
        pytest.param(
            "http://[fd00::5]:9080/maximo/oslc/whoami?token=abc",
            "http://[fd00::5]:9080/maximo/oslc/whoami",
            ("token", "abc"),
            id="ipv6-host",
        ),
        pytest.param(
            "http://[fd00::5:9080/x?token=abc", "<unparseable URL>", ("token", "abc"), id="bad-url"
        ),
        pytest.param(
            "https://maximo.example.com/maximo/oslc/os/mxwo;jsessionid=0000Ab12Cd:-1?pageno=2",
            "https://maximo.example.com/maximo/oslc/os/mxwo;jsessionid=<value>",
            ("0000Ab12Cd", "pageno"),
            id="matrix-parameter",
        ),
        pytest.param(
            "https://sap.example.com/sap/opu/odata/sap/API_MAINTENANCEORDER;v=0002/"
            "MaintenanceOrder('4000123');jsessionid=S3SS10N;flag",
            "https://sap.example.com/sap/opu/odata/sap/API_MAINTENANCEORDER;v=<value>/"
            "MaintenanceOrder(<key>);jsessionid=<value>;<parameter>",
            ("4000123", "S3SS10N", "0002", "flag"),
            id="key-then-matrix-parameter",
        ),
        pytest.param(
            "https://sap.example.com/odata/MaintenanceOrder%28%274000123%27%29",
            "https://sap.example.com/odata/MaintenanceOrder(<key>)",
            ("4000123",),
            id="encoded-key",
        ),
        pytest.param(
            # An unencoded "/" in the password: urllib reads "user" as the host.
            "https://svc_machina:pw/12@cmms.example.com/api/v2/users",
            "<unparseable URL>",
            ("svc_machina", "pw", "12@"),
            id="user-info-split-by-slash",
        ),
        pytest.param(
            # The same with a numeric first half, which urllib takes as a port.
            "https://svc_machina:1234/abc@cmms.example.com/api/v2/users",
            "<unparseable URL>",
            ("svc_machina", "1234", "abc@"),
            id="user-info-split-by-slash-numeric",
        ),
    ],
)
async def test_retry_warnings_log_the_url_without_query_credentials_or_keys(
    events: list[Any], event_name: str, url: str, logged: str, secrets: tuple[str, ...]
) -> None:
    """By default a warning logs scheme, host, port and path: no query (filter
    values, page tokens, Maximo's _lid/_lpwd), no user info, no fragment, and an
    OData key predicate as ``(<key>)``. The request itself is sent unchanged."""
    from structlog.testing import capture_logs

    class _RecordingClient(_SequenceClient):
        sent: list[str]

        async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
            self.sent = [*getattr(self, "sent", []), url]
            return await super().request(method, url, **kwargs)

    client = _RecordingClient(events)
    with capture_logs() as logs:
        await request_with_retry(client, "GET", url, max_backoff=8.0)

    (warning,) = [e for e in logs if e["event"] == event_name]
    assert warning["url"] == logged
    assert warning["method"] == "GET"
    for secret in secrets:
        assert secret not in repr(logs)
    assert set(client.sent) == {url}


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "event_name"), _WARNINGS)
async def test_a_log_url_is_reduced_the_same_way(events: list[Any], event_name: str) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        await request_with_retry(
            _SequenceClient(events),
            "GET",
            _PART_URL,
            max_backoff=8.0,
            log_url="https://u:p@cmms.example.com/api/v2/parts/<listed part>?sku=6205",
        )

    (warning,) = [e for e in logs if e["event"] == event_name]
    assert warning["url"] == "https://cmms.example.com/api/v2/parts/<listed part>"


@pytest.mark.parametrize(
    ("segment", "logged"),
    [
        ("Foo('a;b')", "Foo(<key>)"),
        ("Foo('a;b');jsessionid=X1", "Foo(<key>);jsessionid=<value>"),
        ("Foo%28%27a%27%29;v=2", "Foo(<key>);v=<value>"),
        ("Foo()", "Foo()"),
        ("Foo(", "Foo("),
        ("mxwo;jsessionid=X1", "mxwo;jsessionid=<value>"),
        # A parenthesis inside a matrix parameter's value is not a key.
        ("mxwo;jsessionid=AB(CD)EF", "mxwo;jsessionid=<value>"),
        # Characters whose lowercase is longer must not shift the key's position.
        ("İ" * 10 + "Foo%28%274000123%27%29", "İ" * 10 + "Foo(<key>)"),
    ],
)
def test_a_path_segment_is_reduced_as_one_key(segment: str, logged: str) -> None:
    """A ";" inside a quoted key does not split it; an empty key is no ID."""
    from machina.connectors.cmms.retry import loggable_url

    assert loggable_url(f"https://h/svc/{segment}") == f"https://h/svc/{logged}"


@pytest.mark.parametrize("opener", ["(", "%28"])
def test_a_long_segment_is_reduced_in_linear_time(opener: str) -> None:
    """A server's link can hold a 64k segment of openers with no closer: a
    backtracking pattern took seconds on it (it runs on each logged retry)."""
    import time

    from machina.connectors.cmms.retry import loggable_url

    url = f"https://h/svc/Foo{opener * 65_536}"
    started = time.perf_counter()
    logged = loggable_url(url)
    assert time.perf_counter() - started < 0.5
    assert logged == url
    assert loggable_url(f"{url}x)") == "https://h/svc/Foo(<key>)"


@pytest.mark.asyncio
async def test_a_request_that_needs_no_retry_reduces_no_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL is reduced only for a warning, never on the request path."""
    import machina.connectors.cmms.retry as retry

    def _boom(_url: str) -> str:
        raise AssertionError("reduced without a warning")

    monkeypatch.setattr(retry, "loggable_url", _boom)
    resp = await request_with_retry(_SequenceClient([_FakeResponse(200)]), "GET", _PART_URL)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "url",
    [5, None, True, {"uri": "https://sap.example.com/x?$skiptoken=1"}, b"https://h/x?t=1"],
    ids=["int", "none", "bool", "dict", "bytes"],
)
def test_loggable_url_never_raises(url: object) -> None:
    """It only shapes a log line: a vendor's __next value of another type must
    not turn into an exception before the request (httpx refuses it there)."""
    from machina.connectors.cmms.retry import UNPARSEABLE_URL, loggable_url

    assert loggable_url(url) == UNPARSEABLE_URL  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_network_retry_logs_the_error_type_not_its_text() -> None:
    """httpx's message can name the host, port and address it could not reach."""
    from structlog.testing import capture_logs

    error = httpx.ConnectError("[Errno 111] Connect call failed ('10.20.0.5', 8443) token=abc")
    with capture_logs() as logs:
        await request_with_retry(
            _SequenceClient([error, _FakeResponse(200)]), "GET", "https://example.com/x"
        )

    (warning,) = [e for e in logs if e["event"] == "http_network_retry"]
    assert warning["error_type"] == "ConnectError"
    assert "error" not in warning
    for secret in ("10.20.0.5", "8443", "token=abc", "Errno"):
        assert secret not in repr(logs)
    assert (warning["attempt"], warning["max_retries"]) == (1, 3)
