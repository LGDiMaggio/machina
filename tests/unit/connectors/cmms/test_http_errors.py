"""Unit tests for the shared httpx → connector-error mapping of the REST CMMS connectors."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from structlog.testing import capture_logs

from machina.connectors.cmms._http_errors import (
    WRITE_NOT_SENT,
    WRITE_OUTCOME_UNKNOWN,
    WRITE_REQUEST_NOT_SENT,
    rest_errors,
    status_error,
    write_not_sent,
    write_outcome_unknown,
)
from machina.exceptions import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorTimeoutError,
)

# A URL carrying credentials, as httpx can echo it in its own messages.
_URL = "https://svc:s3cret@cmms.example.com/api/work_orders?apikey=k3y"


def _raise_inside(exc: BaseException, *, write: bool = False) -> None:
    with rest_errors("Acme", "create work order", write=write):
        raise exc


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _URL)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Server error '{status}' for url '{_URL}'", request=request, response=response
    )


def _decode_error(body: bytes) -> ValueError:
    """The error Response.json() raises for this body."""
    try:
        httpx.Response(200, content=body).json()
    except ValueError as exc:
        return exc
    raise AssertionError("the body decoded")


_NOT_JSON = _decode_error(b"<html>login</html>")
_NOT_UTF8 = _decode_error('{"name": "Pompa unità 2"}'.encode("latin-1"))


# --- reads -----------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        httpx.WriteTimeout(""),
        httpx.PoolTimeout(""),
    ],
)
def test_timeout_becomes_connector_timeout_error(exc: httpx.TimeoutException) -> None:
    with pytest.raises(ConnectorTimeoutError) as info:
        _raise_inside(exc)
    assert str(info.value) == "Acme create work order timed out"
    assert info.value.__cause__ is exc


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError(""),
        httpx.ReadError(""),
        httpx.RemoteProtocolError(""),
        httpx.UnsupportedProtocol(""),
    ],
)
def test_other_transport_error_becomes_connector_error(exc: httpx.TransportError) -> None:
    with pytest.raises(ConnectorError) as info:
        _raise_inside(exc)
    assert info.type is ConnectorError
    assert str(info.value) == f"Acme create work order failed: {type(exc).__name__}"
    assert info.value.__cause__ is exc


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_becomes_connector_auth_error(status: int) -> None:
    exc = _status_error(status)
    with pytest.raises(ConnectorAuthError) as info:
        _raise_inside(exc)
    assert str(info.value) == f"Acme create work order failed: HTTP {status}"
    assert info.value.__cause__ is exc


@pytest.mark.parametrize("status", [303, 307, 404, 500])
def test_other_error_status_becomes_connector_error(status: int) -> None:
    with pytest.raises(ConnectorError) as info:
        _raise_inside(_status_error(status))
    assert info.type is ConnectorError
    assert str(info.value) == f"Acme create work order failed: HTTP {status}"


def _body_encoding_error() -> UnicodeEncodeError:
    """The error httpx raises for a JSON body with an unpaired surrogate."""
    try:
        httpx.Request("POST", "https://cmms.example.com/api", json={"description": "a\ud800"})
    except UnicodeEncodeError as exc:
        return exc
    raise AssertionError("the body encoded")


@pytest.mark.parametrize(
    ("write", "message"),
    [
        pytest.param(
            False,
            "Acme create work order failed: a request value cannot be encoded as UTF-8",
            id="read",
        ),
        pytest.param(
            True,
            "Acme create work order failed: a request value cannot be encoded as UTF-8. "
            f"{WRITE_NOT_SENT}",
            id="write",
        ),
    ],
)
def test_value_that_is_not_utf8_becomes_connector_error_without_the_codec_message(
    write: bool, message: str
) -> None:
    """Not a header problem: the message must not point at the credentials."""
    exc = _body_encoding_error()
    assert exc.encoding == "utf-8"
    with pytest.raises(ConnectorError) as info:
        _raise_inside(exc, write=write)
    assert info.type is ConnectorError
    assert str(info.value) == message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def _header_encoding_error() -> UnicodeEncodeError:
    """The error httpx raises for a header value that is not ASCII."""
    try:
        httpx.Request("GET", "https://cmms.example.com/api", headers={"X-Key": "clé"})
    except UnicodeEncodeError as exc:
        return exc
    raise AssertionError("the header encoded")


@pytest.mark.parametrize(
    ("write", "message"),
    [
        pytest.param(
            False,
            "Acme create work order failed: a request header is not ASCII (check the credentials)",
            id="read",
        ),
        pytest.param(
            True,
            "Acme create work order failed: a request header is not "
            f"ASCII (check the credentials). {WRITE_NOT_SENT}",
            id="write",
        ),
    ],
)
def test_header_that_is_not_ascii_becomes_config_error_without_the_codec_message(
    write: bool, message: str
) -> None:
    exc = _header_encoding_error()
    with capture_logs() as logs, pytest.raises(ConnectorConfigError) as info:
        _raise_inside(exc, write=write)
    assert str(info.value) == message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
    assert "é" not in str(info.value)
    assert logs == []


def test_invalid_url_becomes_connector_config_error() -> None:
    with pytest.raises(ConnectorConfigError) as info, rest_errors("Acme", "health check"):
        httpx.URL("https://cmms.example.com:notaport/api")
    assert str(info.value) == (
        "Acme health check failed: invalid URL (check 'url' and the endpoint paths)"
    )
    assert isinstance(info.value.__cause__, httpx.InvalidURL)


@pytest.mark.parametrize(
    ("exc", "problem"),
    [
        pytest.param(_NOT_JSON, "not JSON", id="not-json"),
        pytest.param(_NOT_UTF8, "not UTF-8", id="not-utf-8"),
    ],
)
def test_undecodable_body_becomes_connector_error(exc: ValueError, problem: str) -> None:
    assert isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError))
    with pytest.raises(ConnectorError) as info, rest_errors("Acme", "read assets"):
        raise exc
    assert info.type is ConnectorError
    assert str(info.value) == f"Acme read assets failed: response is {problem}"
    assert info.value.__cause__ is exc


# --- writes ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ConnectorError),
        (401, ConnectorAuthError),
        (403, ConnectorAuthError),
        (409, ConnectorError),
        (422, ConnectorError),
    ],
)
def test_refused_write_fails(status: int, expected: type[ConnectorError]) -> None:
    """A 4xx status is the CMMS refusing the write: a failure, as for a read."""
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        _raise_inside(_status_error(status), write=True)
    assert info.type is expected
    assert str(info.value) == f"Acme create work order failed: HTTP {status}"
    assert logs == []


@pytest.mark.parametrize("status", [101, 301, 302, 303, 307, 308, 500, 502, 503, 504])
def test_status_other_than_a_refusal_leaves_the_write_outcome_unknown(status: int) -> None:
    """Only a 4xx refuses a write: a 303 See Other answers an applied one."""
    exc = _status_error(status)
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        _raise_inside(exc, write=True)
    assert info.type is ConnectorError
    assert str(info.value) == (
        f"Acme create work order was answered with HTTP {status}. {WRITE_OUTCOME_UNKNOWN}"
    )
    assert info.value.__cause__ is exc
    assert logs == [
        {
            "event": "rest_write_outcome_unknown",
            "connector": "Acme",
            "operation": "create work order",
            "log_level": "warning",
        }
    ]


@pytest.mark.parametrize(
    ("exc", "expected", "what_happened"),
    [
        pytest.param(
            httpx.ConnectError(""),
            ConnectorError,
            "failed: ConnectError",
            id="connect-error",
        ),
        pytest.param(
            httpx.UnsupportedProtocol(""),
            ConnectorError,
            "failed: UnsupportedProtocol",
            id="unsupported-protocol",
        ),
        pytest.param(
            httpx.ConnectTimeout(""),
            ConnectorTimeoutError,
            "timed out",
            id="connect-timeout",
        ),
        pytest.param(
            httpx.PoolTimeout(""),
            ConnectorTimeoutError,
            "timed out",
            id="pool-timeout",
        ),
    ],
)
def test_write_that_failed_before_sending_says_no_request_was_sent(
    exc: httpx.TransportError, expected: type[ConnectorError], what_happened: str
) -> None:
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        _raise_inside(exc, write=True)
    assert info.type is expected
    assert str(info.value) == f"Acme create work order {what_happened}. {WRITE_NOT_SENT}"
    assert info.value.__cause__ is exc
    assert logs == []


@pytest.mark.parametrize(
    ("exc", "expected", "what_happened"),
    [
        pytest.param(
            httpx.ReadTimeout(""),
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
            httpx.ReadError(""),
            ConnectorError,
            "was interrupted after sending began: ReadError",
            id="read-error",
        ),
        pytest.param(
            httpx.WriteError(""),
            ConnectorError,
            "was interrupted after sending began: WriteError",
            id="write-error",
        ),
        pytest.param(
            httpx.RemoteProtocolError(""),
            ConnectorError,
            "was interrupted after sending began: RemoteProtocolError",
            id="remote-protocol-error",
        ),
        pytest.param(
            # h11 can refuse a request before or after its headers went out.
            httpx.LocalProtocolError(""),
            ConnectorError,
            "was interrupted after sending began: LocalProtocolError",
            id="local-protocol-error",
        ),
        pytest.param(
            httpx.ProxyError(""),
            ConnectorError,
            "was interrupted after sending began: ProxyError",
            id="proxy-error",
        ),
        pytest.param(
            httpx.DecodingError(""),
            ConnectorError,
            "was interrupted after sending began: DecodingError",
            id="decoding-error",
        ),
    ],
)
def test_write_that_failed_after_sending_began_leaves_the_outcome_unknown(
    exc: httpx.HTTPError, expected: type[ConnectorError], what_happened: str
) -> None:
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        _raise_inside(exc, write=True)
    assert info.type is expected
    assert str(info.value) == f"Acme create work order {what_happened}. {WRITE_OUTCOME_UNKNOWN}"
    assert info.value.__cause__ is exc
    assert [e["event"] for e in logs] == ["rest_write_outcome_unknown"]


def test_write_with_an_invalid_url_says_no_request_was_sent() -> None:
    with (
        pytest.raises(ConnectorConfigError) as info,
        rest_errors("Acme", "create work order", write=True),
    ):
        httpx.URL("https://cmms.example.com:notaport/api")
    assert str(info.value) == (
        "Acme create work order failed: invalid URL "
        f"(check 'url' and the endpoint paths). {WRITE_NOT_SENT}"
    )


@pytest.mark.parametrize(
    ("exc", "problem"),
    [
        pytest.param(_NOT_JSON, "not JSON", id="not-json"),
        pytest.param(_NOT_UTF8, "not UTF-8", id="not-utf-8"),
    ],
)
def test_undecodable_write_response_leaves_the_outcome_unknown(
    exc: ValueError, problem: str
) -> None:
    """A 2xx answered the write, so the CMMS may have applied it."""
    with pytest.raises(ConnectorError) as info:
        _raise_inside(exc, write=True)
    assert info.type is ConnectorError
    assert str(info.value) == (
        f"Acme create work order was answered with a success status, but the response is "
        f"{problem}. {WRITE_OUTCOME_UNKNOWN}"
    )
    assert info.value.__cause__ is exc


def test_write_outcome_unknown_builds_the_requested_error_type() -> None:
    with capture_logs() as logs:
        error = write_outcome_unknown(
            "Acme", "update work order", "did something", error_type=ConnectorTimeoutError
        )
    assert type(error) is ConnectorTimeoutError
    assert str(error) == f"Acme update work order did something. {WRITE_OUTCOME_UNKNOWN}"
    assert [e["event"] for e in logs] == ["rest_write_outcome_unknown"]


def test_the_unknown_outcome_wording_is_the_approved_contract() -> None:
    assert WRITE_OUTCOME_UNKNOWN == (
        "The write operation's outcome is unknown. It may have been applied. "
        "Check the CMMS before retrying."
    )
    assert WRITE_NOT_SENT == "No request was sent to the CMMS."
    assert WRITE_REQUEST_NOT_SENT == "The write request was not sent to the CMMS."


# --- vendor helpers ----------------------------------------------------------


@pytest.mark.parametrize("status", [301, 303, 401, 403, 404, 422, 500, 503])
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
def test_status_error_is_the_error_rest_errors_raises(status: int, write: bool) -> None:
    """The vendor connectors check statuses themselves; they must answer alike."""
    with capture_logs(), pytest.raises(ConnectorError) as raised:
        _raise_inside(_status_error(status), write=write)
    with capture_logs() as logs:
        built = status_error("Acme", "create work order", status, write=write)
    assert type(built) is raised.type
    assert str(built) == str(raised.value)
    unknown = write and not 400 <= status < 500
    assert [e["event"] for e in logs] == (["rest_write_outcome_unknown"] if unknown else [])


@pytest.mark.parametrize(
    ("exc", "expected", "what_happened"),
    [
        pytest.param(httpx.ConnectError(""), ConnectorError, "failed: ConnectError", id="connect"),
        pytest.param(httpx.PoolTimeout(""), ConnectorTimeoutError, "timed out", id="pool-timeout"),
    ],
)
def test_write_can_name_another_not_sent_sentence(
    exc: httpx.TransportError, expected: type[ConnectorError], what_happened: str
) -> None:
    with (
        pytest.raises(ConnectorError) as info,
        rest_errors("Acme", "create work order", write=True, not_sent=WRITE_REQUEST_NOT_SENT),
    ):
        raise exc
    assert info.type is expected
    assert str(info.value) == f"Acme create work order {what_happened}. {WRITE_REQUEST_NOT_SENT}"


def test_not_sent_sentence_does_not_change_an_unknown_outcome() -> None:
    with (
        pytest.raises(ConnectorError) as info,
        rest_errors("Acme", "create work order", write=True, not_sent=WRITE_REQUEST_NOT_SENT),
    ):
        raise httpx.ReadError("")
    assert str(info.value).endswith(WRITE_OUTCOME_UNKNOWN)


@pytest.mark.parametrize(
    "error",
    [
        ConnectorTimeoutError("Acme GET token timed out"),
        ConnectorAuthError("Acme authentication failed."),
        ConnectorError("Acme GET token failed: HTTP 500"),
    ],
)
def test_write_not_sent_keeps_the_type_and_adds_one_sentence(error: ConnectorError) -> None:
    restated = write_not_sent(error)
    assert type(restated) is type(error)
    assert str(restated) == f"{str(error).rstrip('.')}. {WRITE_REQUEST_NOT_SENT}"


# --- both ------------------------------------------------------------------


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError(f"cannot connect to {_URL}"),
        httpx.ReadTimeout(f"timed out reading {_URL}"),
        httpx.ReadError(f"connection reset reading {_URL}"),
        _status_error(500),
        _status_error(422),
    ],
)
def test_message_never_carries_the_url_or_credentials(exc: httpx.HTTPError, write: bool) -> None:
    with capture_logs() as logs, pytest.raises(ConnectorError) as info:
        _raise_inside(exc, write=write)
    for fragment in ("cmms.example.com", "s3cret", "k3y"):
        assert fragment not in str(info.value)
        assert fragment not in repr(logs)


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize(
    "exc",
    [
        ConnectorAuthError("Acme authentication failed"),
        ValueError("response body is not JSON"),
        KeyError("sku"),
        asyncio.CancelledError(),
    ],
)
def test_other_exceptions_pass_through_unchanged(exc: BaseException, write: bool) -> None:
    with pytest.raises(type(exc)) as info:
        _raise_inside(exc, write=write)
    assert info.value is exc


def test_nothing_raised_is_nothing_reported() -> None:
    with capture_logs() as logs, rest_errors("Acme", "create work order", write=True):
        pass
    assert logs == []
