"""Write outcomes of the vendor CMMS connectors (SAP PM, Maximo, UpKeep).

Each domain write either failed before its request was sent (the message says
the write request was not sent), was refused by the CMMS (a 4xx), or has an
outcome the caller cannot know: a 3xx or 5xx, a timeout or a broken
connection after sending began, or a success response that cannot be read.
Its message then says the write may have been applied and that the CMMS must
be checked before retrying. Whatever happens, the write request is sent
exactly once (after a 429, once more), and no message carries the host, a
credential or the CMMS's response body. Neither does a log entry, except the
retry helper's warnings, which log the request URL and are not checked here.

All HTTP traffic is intercepted by pytest-httpx — no real vendor API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from machina.connectors.cmms._http_errors import WRITE_OUTCOME_UNKNOWN, WRITE_REQUEST_NOT_SENT
from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.connectors.cmms.sap_pm import SapPmConnector
from machina.connectors.cmms.upkeep import UpKeepConnector
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType
from machina.exceptions import ConnectorAuthError, ConnectorError, ConnectorTimeoutError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_SAP = "https://sapgw.example.com/sap/opu/odata/sap"
_MAXIMO = "https://maximo.example.com"
_UPKEEP = "https://upkeep.example.com"

_REST_ID = "_V08tMDAxL0hJTExUT1A-"
_MAXIMO_LOOKUP = httpx.URL(
    f"{_MAXIMO}/maximo/oslc/os/mxwo",
    params={
        "oslc.pageSize": "2",
        "lean": "1",
        "oslc.where": 'wonum="WO-001"',
        "oslc.select": "wonum,siteid",
    },
)
_SAP_CSRF = f"{_SAP}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1"

# Values that must never reach a message or a log entry: hosts (including
# the internal one a vendor body names) and credentials.
_SECRETS = (
    "sapgw.example.com",
    "maximo.example.com",
    "upkeep.example.com",
    "maximo-internal",
    "s3cret-sap",
    "k3y-maximo",
    "t0ken-upkeep",
    "csrf-t0k",
    "BODY-VALUE",
)

# An error body as a vendor might send it: an internal URL and a record value.
_VENDOR_BODY = {
    "Error": {
        "message": "BMXAA0000E BODY-VALUE",
        "extendedError": {"moreInfo": {"href": "http://maximo-internal:9080/maximo/oslc/x"}},
    }
}


def _new_work_order() -> WorkOrder:
    now = datetime.now(tz=UTC)
    return WorkOrder(
        id="TEMP",
        type=WorkOrderType.CORRECTIVE,
        priority=Priority.HIGH,
        asset_id="P-201",
        description="Replace bearing",
        created_at=now,
        updated_at=now,
    )


@dataclass(frozen=True)
class _Write:
    """One domain write of one vendor connector, and how to reach it."""

    vendor: str
    operation: str
    method: str
    url: httpx.URL | str
    connector: Callable[[], Any]
    connect: Callable[[Any], None]
    prepare: Callable[[Any], None]
    call: Callable[[Any], Awaitable[Any]]
    is_create: bool


def _sap() -> SapPmConnector:
    return SapPmConnector(url=_SAP, auth=BasicAuth(username="svc", password="s3cret-sap"))


def _maximo() -> MaximoConnector:
    return MaximoConnector(
        url=_MAXIMO, auth=ApiKeyHeaderAuth(header_name="apikey", value="k3y-maximo")
    )


def _upkeep() -> UpKeepConnector:
    return UpKeepConnector(url=_UPKEEP, api_key="t0ken-upkeep")


def _sap_connect(httpx_mock: Any) -> None:
    httpx_mock.add_response(method="GET", url=f"{_SAP}/API_EQUIPMENT/$metadata", text="<x/>")


def _maximo_connect(httpx_mock: Any) -> None:
    httpx_mock.add_response(method="GET", url=f"{_MAXIMO}/maximo/oslc/whoami", json={})


def _upkeep_connect(httpx_mock: Any) -> None:
    httpx_mock.add_response(method="GET", url=f"{_UPKEEP}/api/v2/users?limit=1", json={})


def _sap_csrf(httpx_mock: Any) -> None:
    httpx_mock.add_response(method="GET", url=_SAP_CSRF, headers={"x-csrf-token": "csrf-t0k"})


def _maximo_lookup(httpx_mock: Any) -> None:
    href = f"http://maximo-internal:9080/maximo/oslc/os/mxwo/{_REST_ID}"
    httpx_mock.add_response(
        method="GET", url=_MAXIMO_LOOKUP, json={"member": [{"wonum": "WO-001", "href": href}]}
    )


def _nothing(_httpx_mock: Any) -> None:
    return None


_WRITES = [
    pytest.param(
        _Write(
            vendor="SAP PM",
            operation="create maintenance order",
            method="POST",
            url=f"{_SAP}/API_MAINTENANCEORDER/MaintenanceOrder",
            connector=_sap,
            connect=_sap_connect,
            prepare=_sap_csrf,
            call=lambda c: c.create_work_order(_new_work_order()),
            is_create=True,
        ),
        id="sap-create",
    ),
    pytest.param(
        _Write(
            vendor="SAP PM",
            operation="update maintenance order",
            method="PATCH",
            url=f"{_SAP}/API_MAINTENANCEORDER/MaintenanceOrder('4000001')",
            connector=_sap,
            connect=_sap_connect,
            prepare=_sap_csrf,
            call=lambda c: c.update_work_order("4000001", description="x"),
            is_create=False,
        ),
        id="sap-update",
    ),
    pytest.param(
        _Write(
            vendor="Maximo",
            operation="create work order",
            method="POST",
            url=f"{_MAXIMO}/maximo/oslc/os/mxwo",
            connector=_maximo,
            connect=_maximo_connect,
            prepare=_nothing,
            call=lambda c: c.create_work_order(_new_work_order()),
            is_create=True,
        ),
        id="maximo-create",
    ),
    pytest.param(
        _Write(
            vendor="Maximo",
            operation="update work order",
            method="POST",
            url=httpx.URL(f"{_MAXIMO}/maximo/oslc/os/mxwo/{_REST_ID}", params={"lean": "1"}),
            connector=_maximo,
            connect=_maximo_connect,
            prepare=_maximo_lookup,
            call=lambda c: c.update_work_order("WO-001", description="x"),
            is_create=False,
        ),
        id="maximo-update",
    ),
    pytest.param(
        _Write(
            vendor="UpKeep",
            operation="create work order",
            method="POST",
            url=f"{_UPKEEP}/api/v2/work-orders",
            connector=_upkeep,
            connect=_upkeep_connect,
            prepare=_nothing,
            call=lambda c: c.create_work_order(_new_work_order()),
            is_create=True,
        ),
        id="upkeep-create",
    ),
    pytest.param(
        _Write(
            vendor="UpKeep",
            operation="update work order",
            method="PATCH",
            url=f"{_UPKEEP}/api/v2/work-orders/wo1",
            connector=_upkeep,
            connect=_upkeep_connect,
            prepare=_nothing,
            call=lambda c: c.update_work_order("wo1", description="x"),
            is_create=False,
        ),
        id="upkeep-update",
    ),
]


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _no_sleep)


async def _attempt(httpx_mock: Any, write: _Write) -> tuple[ConnectorError, list[dict[str, Any]]]:
    """Connect, register the write's preparation, run it, and return its error and logs."""
    conn = write.connector()
    write.connect(httpx_mock)
    await conn.connect()
    write.prepare(httpx_mock)
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        await write.call(conn)
    return info.value, logs


def _write_requests(httpx_mock: Any, write: _Write) -> list[httpx.Request]:
    return [
        r
        for r in httpx_mock.get_requests(method=write.method)
        if r.url == httpx.URL(str(write.url))
    ]


def _assert_value_free(error: ConnectorError, logs: list[dict[str, Any]]) -> None:
    for secret in _SECRETS:
        assert secret not in str(error)
        assert secret not in repr(logs)


def _unknown_logged(logs: list[dict[str, Any]]) -> bool:
    return "rest_write_outcome_unknown" in [e["event"] for e in logs]


@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize(
    ("exc", "expected", "what_happened"),
    [
        pytest.param(
            httpx.ConnectError("refused"),
            ConnectorError,
            "failed: ConnectError",
            id="connect-error",
        ),
        pytest.param(
            httpx.ConnectTimeout(""), ConnectorTimeoutError, "timed out", id="connect-timeout"
        ),
        pytest.param(httpx.PoolTimeout(""), ConnectorTimeoutError, "timed out", id="pool-timeout"),
    ],
)
@pytest.mark.asyncio
async def test_failure_before_sending_says_the_write_request_was_not_sent(
    httpx_mock, write: _Write, exc: httpx.TransportError, expected: type, what_happened: str
) -> None:
    httpx_mock.add_exception(exc, method=write.method, url=write.url)
    error, logs = await _attempt(httpx_mock, write)
    assert type(error) is expected
    assert (
        str(error) == f"{write.vendor} {write.operation} {what_happened}. {WRITE_REQUEST_NOT_SENT}"
    )
    assert len(_write_requests(httpx_mock, write)) == 1
    assert not _unknown_logged(logs)
    _assert_value_free(error, logs)


@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize(
    ("exc", "expected", "what_happened"),
    [
        pytest.param(
            httpx.ReadTimeout("timed out reading https://svc:s3cret-sap@sapgw.example.com"),
            ConnectorTimeoutError,
            "timed out after sending began",
            id="read-timeout",
        ),
        pytest.param(
            httpx.WriteTimeout(""),
            ConnectorTimeoutError,
            "timed out after sending began",
            id="write-timeout",
        ),
        pytest.param(
            httpx.ReadError("connection reset by maximo.example.com"),
            ConnectorError,
            "was interrupted after sending began: ReadError",
            id="read-error",
        ),
        pytest.param(
            httpx.RemoteProtocolError("peer closed"),
            ConnectorError,
            "was interrupted after sending began: RemoteProtocolError",
            id="remote-protocol-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_failure_after_sending_began_is_sent_once_and_may_have_applied(
    httpx_mock, write: _Write, exc: httpx.HTTPError, expected: type, what_happened: str
) -> None:
    httpx_mock.add_exception(exc, method=write.method, url=write.url)
    error, logs = await _attempt(httpx_mock, write)
    assert type(error) is expected
    assert (
        str(error) == f"{write.vendor} {write.operation} {what_happened}. {WRITE_OUTCOME_UNKNOWN}"
    )
    assert len(_write_requests(httpx_mock, write)) == 1
    assert _unknown_logged(logs)
    _assert_value_free(error, logs)


@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_status_other_than_a_refusal_is_sent_once_and_may_have_applied(
    httpx_mock, write: _Write, status: int
) -> None:
    """A redirect is not followed (303 See Other can answer an applied write),
    and a 5xx can come after the CMMS committed the write; 503 included, which
    is not retried for a write."""
    httpx_mock.add_response(
        method=write.method, url=write.url, status_code=status, json=_VENDOR_BODY
    )
    error, logs = await _attempt(httpx_mock, write)
    assert type(error) is ConnectorError
    assert str(error) == (
        f"{write.vendor} {write.operation} was answered with HTTP {status}. {WRITE_OUTCOME_UNKNOWN}"
    )
    assert len(_write_requests(httpx_mock, write)) == 1
    assert _unknown_logged(logs)
    _assert_value_free(error, logs)


@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ConnectorError),
        (403, ConnectorAuthError),
        (404, ConnectorError),
        (409, ConnectorError),
        (422, ConnectorError),
    ],
)
@pytest.mark.asyncio
async def test_refusal_is_a_plain_failure(
    httpx_mock, write: _Write, status: int, expected: type
) -> None:
    httpx_mock.add_response(
        method=write.method, url=write.url, status_code=status, json=_VENDOR_BODY
    )
    error, logs = await _attempt(httpx_mock, write)
    assert type(error) is expected
    assert str(error) == f"{write.vendor} {write.operation} failed: HTTP {status}"
    assert len(_write_requests(httpx_mock, write)) == 1
    assert not _unknown_logged(logs)
    _assert_value_free(error, logs)


@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.asyncio
async def test_429_is_retried_and_its_retry_reported_alike(httpx_mock, write: _Write) -> None:
    """A 429 is refused unprocessed, so even a write is retried; a retry that
    then fails after sending is reported as unknown, like a first attempt."""
    httpx_mock.add_response(method=write.method, url=write.url, status_code=429)
    httpx_mock.add_exception(httpx.ReadTimeout(""), method=write.method, url=write.url)
    error, _logs = await _attempt(httpx_mock, write)
    assert type(error) is ConnectorTimeoutError
    assert str(error).endswith(WRITE_OUTCOME_UNKNOWN)
    assert len(_write_requests(httpx_mock, write)) == 2


_CREATES = [p for p in _WRITES if p.values[0].is_create]


@pytest.mark.parametrize("write", _CREATES)
@pytest.mark.parametrize(
    ("content", "problem"),
    [
        pytest.param(
            b"<html>Sign in at maximo-internal BODY-VALUE</html>", "not JSON", id="not-json"
        ),
        pytest.param('{"id": "BODY-VALUE unità"}'.encode("latin-1"), "not UTF-8", id="not-utf-8"),
    ],
)
@pytest.mark.asyncio
async def test_create_answered_with_a_body_that_is_not_json_may_have_applied(
    httpx_mock, write: _Write, content: bytes, problem: str
) -> None:
    """A proxy's login page can answer 200: the create may or may not have
    reached the CMMS."""
    httpx_mock.add_response(method=write.method, url=write.url, status_code=201, content=content)
    error, logs = await _attempt(httpx_mock, write)
    assert type(error) is ConnectorError
    assert str(error) == (
        f"{write.vendor} {write.operation} was answered with a success status, but the "
        f"response is {problem}. {WRITE_OUTCOME_UNKNOWN}"
    )
    assert len(_write_requests(httpx_mock, write)) == 1
    _assert_value_free(error, logs)


_PREPARED = [
    pytest.param(_SAP_CSRF, "SAP PM CSRF token fetch", p.values[0], id=f"{p.id}-csrf-fetch")
    for p in _WRITES
    if p.values[0].vendor == "SAP PM"
] + [
    pytest.param(_MAXIMO_LOOKUP, "Maximo GET mxwo", p.values[0], id=f"{p.id}-lookup")
    for p in _WRITES
    if p.values[0].vendor == "Maximo" and not p.values[0].is_create
]


@pytest.mark.parametrize(("url", "step", "write"), _PREPARED)
@pytest.mark.parametrize(
    ("exc", "expected", "what_happened"),
    [
        pytest.param(httpx.ReadTimeout(""), ConnectorTimeoutError, "timed out", id="timeout"),
        pytest.param(
            httpx.RemoteProtocolError(""),
            ConnectorError,
            "failed: RemoteProtocolError",
            id="protocol",
        ),
    ],
)
@pytest.mark.asyncio
async def test_failed_read_before_the_write_says_the_write_request_was_not_sent(
    httpx_mock,
    url: httpx.URL | str,
    step: str,
    write: _Write,
    exc: httpx.HTTPError,
    expected: type,
    what_happened: str,
) -> None:
    """SAP PM fetches a CSRF token and Maximo looks the order up before the
    write: the read is retried as any read, and when it still fails the write
    request was never sent, however the read failed."""
    conn = write.connector()
    write.connect(httpx_mock)
    await conn.connect()
    httpx_mock.add_exception(exc, method="GET", url=url, is_reusable=True)
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        await write.call(conn)
    assert type(info.value) is expected
    assert str(info.value) == f"{step} {what_happened}. {WRITE_REQUEST_NOT_SENT}"
    assert _write_requests(httpx_mock, write) == []
    assert not _unknown_logged(logs)
    # Only the message: the read's retry warnings log its URL, as every
    # retried read's do.
    _assert_value_free(info.value, [])
