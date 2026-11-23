"""Retry warnings of the SAP PM connector, through ``request_with_retry``'s default.

The SAP PM connector passes no ``log_url``. Its next-page links carry the
query (``$skiptoken``, the read's ``$filter``) and its updates address the order
by an OData key predicate, so the retry helper's default must keep both out of
the ``http_*`` warnings. All HTTP traffic is intercepted by pytest-httpx.
"""

from __future__ import annotations

import httpx
import pytest
import structlog.testing

from machina.connectors.cmms.auth import BasicAuth
from machina.connectors.cmms.sap_pm import SapPmConnector

BASE = "https://sap.example.com/sap/opu/odata/sap"
_ODATA_PARAMS = {"$top": "100", "$format": "json"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _sleep)


@pytest.fixture
def connector() -> SapPmConnector:
    return SapPmConnector(
        url=BASE, auth=BasicAuth(username="sapuser", password="secret"), sap_client="100"
    )


async def _connect(httpx_mock, conn: SapPmConnector) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{BASE}/API_EQUIPMENT/$metadata", text="<edmx:Edmx/>"
    )
    await conn.connect()


def _retries(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if str(e["event"]).startswith("http_")]


@pytest.mark.asyncio
async def test_a_retried_next_page_logs_no_query(httpx_mock, connector: SapPmConnector) -> None:
    await _connect(httpx_mock, connector)
    next_url = f"{BASE}/API_EQUIPMENT/Equipment?$skiptoken=100&$filter=Equipment eq '10000077'"
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(f"{BASE}/API_EQUIPMENT/Equipment", params=_ODATA_PARAMS),
        json={"d": {"results": [{"Equipment": "A1"}], "__next": next_url}},
    )
    httpx_mock.add_response(method="GET", url=next_url, status_code=429)
    httpx_mock.add_response(
        method="GET", url=next_url, json={"d": {"results": [{"Equipment": "A2"}]}}
    )

    with structlog.testing.capture_logs() as logs:
        assets = await connector.read_assets()

    assert [a.id for a in assets] == ["A1", "A2"]
    (retry,) = _retries(logs)
    assert (retry["event"], retry["url"]) == (
        "http_rate_limit_retry",
        f"{BASE}/API_EQUIPMENT/Equipment",
    )
    for secret in ("10000077", "skiptoken", "$filter"):
        assert secret not in repr(retry)


@pytest.mark.asyncio
async def test_a_rate_limited_order_update_logs_no_order_key(
    httpx_mock, connector: SapPmConnector
) -> None:
    """The CSRF fetch and the PATCH are both retried on 429; neither warning
    names the order, whose key sits in the PATCH URL's path."""
    await _connect(httpx_mock, connector)
    csrf_url = f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder?$top=1"
    httpx_mock.add_response(method="GET", url=csrf_url, status_code=429)
    httpx_mock.add_response(
        method="GET", url=csrf_url, headers={"x-csrf-token": "csrf-1"}, json={"d": {"results": []}}
    )
    patch_url = f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder('4000777')"
    httpx_mock.add_response(method="PATCH", url=patch_url, status_code=429)
    httpx_mock.add_response(method="PATCH", url=patch_url, status_code=204)

    with structlog.testing.capture_logs() as logs:
        resp = await connector._write_with_csrf(
            "PATCH", patch_url, {"MaintenanceOrderDesc": "Replace seal"}, operation="update"
        )

    assert resp.status_code == 204
    assert [(e["method"], e["url"]) for e in _retries(logs)] == [
        ("GET", f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder"),
        ("PATCH", f"{BASE}/API_MAINTENANCEORDER/MaintenanceOrder(<key>)"),
    ]
    for secret in ("4000777", "$top", "csrf-1"):
        assert secret not in repr(_retries(logs))
    assert len(httpx_mock.get_requests(method="PATCH")) == 2  # retry count unchanged
