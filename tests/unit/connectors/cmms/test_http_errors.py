"""Unit tests for the shared httpx → connector-error mapping of the REST CMMS connectors."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from machina.connectors.cmms._http_errors import rest_errors
from machina.exceptions import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorTimeoutError,
)

# A URL carrying credentials, as httpx can echo it in its own messages.
_URL = "https://svc:s3cret@cmms.example.com/api/work_orders?apikey=k3y"


def _raise_inside(exc: BaseException) -> None:
    with rest_errors("Acme", "create work order"):
        raise exc


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _URL)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Server error '{status}' for url '{_URL}'", request=request, response=response
    )


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


def test_other_error_status_becomes_connector_error() -> None:
    with pytest.raises(ConnectorError) as info:
        _raise_inside(_status_error(500))
    assert info.type is ConnectorError
    assert str(info.value) == "Acme create work order failed: HTTP 500"


def test_invalid_url_becomes_connector_config_error() -> None:
    with pytest.raises(ConnectorConfigError) as info, rest_errors("Acme", "health check"):
        httpx.URL("https://cmms.example.com:notaport/api")
    assert str(info.value).startswith("Acme health check failed: invalid URL")
    assert isinstance(info.value.__cause__, httpx.InvalidURL)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError(f"cannot connect to {_URL}"),
        httpx.ReadTimeout(f"timed out reading {_URL}"),
        _status_error(500),
    ],
)
def test_message_never_carries_the_url_or_credentials(exc: httpx.HTTPError) -> None:
    with pytest.raises(ConnectorError) as info:
        _raise_inside(exc)
    for fragment in ("cmms.example.com", "s3cret", "k3y"):
        assert fragment not in str(info.value)


@pytest.mark.parametrize(
    "exc",
    [
        ConnectorAuthError("Acme authentication failed"),
        ValueError("response body is not JSON"),
        asyncio.CancelledError(),
    ],
)
def test_other_exceptions_pass_through_unchanged(exc: BaseException) -> None:
    with pytest.raises(type(exc)) as info:
        _raise_inside(exc)
    assert info.value is exc
