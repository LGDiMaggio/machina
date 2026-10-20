"""Tests for vendor-specific MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
import structlog.testing

from machina.connectors.base import set_sandbox_mode
from machina.connectors.cmms.auth import ApiKeyHeaderAuth, BasicAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.connectors.cmms.sap_pm import SapPmConnector
from machina.mcp.tools_vendor import (
    VENDOR_TOOLS,
    maximo_raw_attribute_update,
    sap_pm_raw_iw38_notification,
)
from machina.runtime import MachinaRuntime

_MAXIMO_URL = "https://maximo.example.com"
_SAP_URL = "https://sap.example.com/sap/opu/odata/sap"


def _ctx(runtime: MachinaRuntime) -> MagicMock:
    """A FastMCP request context whose lifespan holds ``runtime``."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"runtime": runtime}
    return ctx


def _maximo_ctx(*, sandbox_mode: bool = False, connected: bool = True) -> MagicMock:
    """The context of a server with one Maximo connector, connected by default."""
    conn = MaximoConnector(url=_MAXIMO_URL, auth=ApiKeyHeaderAuth(header_name="apikey", value="k"))
    conn._connected = connected
    return _ctx(MachinaRuntime(connectors={"maximo": conn}, sandbox_mode=sandbox_mode))


def _sap_ctx(*, sandbox_mode: bool = False, connected: bool = True) -> MagicMock:
    """The context of a server with one SAP PM connector, connected by default."""
    conn = SapPmConnector(url=_SAP_URL, auth=BasicAuth(username="u", password="p"))
    conn._connected = connected
    return _ctx(MachinaRuntime(connectors={"sap_pm": conn}, sandbox_mode=sandbox_mode))


class TestVendorToolsList:
    def test_vendor_tools_registered(self) -> None:
        names = [t.__name__ for t in VENDOR_TOOLS]
        assert "sap_pm_raw_iw38_notification" in names
        assert "maximo_raw_attribute_update" in names

    def test_vendor_tools_count(self) -> None:
        assert len(VENDOR_TOOLS) == 2


class TestVendorToolsNotRegisteredByDefault:
    def test_build_server_without_vendor_tools(self) -> None:
        from machina.config.schema import MachinaConfig
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert "sap_pm_raw_iw38_notification" not in tool_names
        assert "maximo_raw_attribute_update" not in tool_names

    def test_build_server_with_vendor_tools_enabled(self) -> None:
        from machina.config.schema import MachinaConfig, McpConfig
        from machina.mcp.server import build_server

        config = MachinaConfig(mcp=McpConfig(enable_vendor_tools=True))
        server = build_server(config)
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert "sap_pm_raw_iw38_notification" in tool_names
        assert "maximo_raw_attribute_update" in tool_names


class TestSapRawNotificationNoConnector:
    @pytest.mark.asyncio
    async def test_returns_error_without_sap(self) -> None:
        from machina.mcp.tools_vendor import sap_pm_raw_iw38_notification
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime()
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}
        result = await sap_pm_raw_iw38_notification(ctx, equipment_id="EQ-1", description="test")
        assert "error" in result


class TestMaximoRawUpdateNoConnector:
    @pytest.mark.asyncio
    async def test_returns_error_without_maximo(self) -> None:
        from machina.mcp.tools_vendor import maximo_raw_attribute_update
        from machina.runtime import MachinaRuntime

        runtime = MachinaRuntime()
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"runtime": runtime}
        result = await maximo_raw_attribute_update(ctx, resource_type="mxwo", resource_id="WO-1")
        assert "error" in result


class TestVendorToolsSandboxPropagation:
    """A sandbox-mode server must block raw vendor writes even when the
    per-request task did not inherit the sandbox contextvar. Regression:
    the vendor tools read get_sandbox_mode() before _runtime() re-established
    it, and the Maximo raw httpx PATCH had no @sandbox_aware backstop — so the
    write executed live in sandbox mode."""

    @pytest.mark.asyncio
    async def test_maximo_raw_blocked_in_sandbox(self) -> None:
        from machina.connectors.base import set_sandbox_mode
        from machina.mcp.tools_vendor import maximo_raw_attribute_update
        from machina.runtime import MachinaRuntime

        set_sandbox_mode(False)  # simulate a request task that did not inherit it
        try:
            runtime = MachinaRuntime(sandbox_mode=True)
            ctx = MagicMock()
            ctx.request_context.lifespan_context = {"runtime": runtime}
            result = await maximo_raw_attribute_update(
                ctx, resource_type="mxwo", resource_id="WO-1", attributes={"status": "x"}
            )
            assert result["metadata"]["sandbox"] is True
        finally:
            set_sandbox_mode(False)

    @pytest.mark.asyncio
    async def test_sap_raw_blocked_in_sandbox(self) -> None:
        from machina.connectors.base import set_sandbox_mode
        from machina.mcp.tools_vendor import sap_pm_raw_iw38_notification
        from machina.runtime import MachinaRuntime

        set_sandbox_mode(False)
        try:
            runtime = MachinaRuntime(sandbox_mode=True)
            ctx = MagicMock()
            ctx.request_context.lifespan_context = {"runtime": runtime}
            result = await sap_pm_raw_iw38_notification(
                ctx, equipment_id="EQ-1", description="test"
            )
            assert result["metadata"]["sandbox"] is True
        finally:
            set_sandbox_mode(False)


# The fixed refusals: each names the rule, never the caller's value.
_OBJECT_STRUCTURE_REFUSAL = (
    "Invalid Maximo object structure: expected an object structure name "
    "such as 'mxwo' (ASCII letters, digits and underscores only)"
)
_RECORD_ID_REFUSAL = (
    "Invalid record ID: an ID cannot be empty, and no part of it between "
    "'/' or '\\' separators can be empty, '.' or '..', including before a ';'"
)


class TestMaximoRawUpdateStaysOnOneResource:
    """``resource_type`` and ``resource_id`` come from the MCP client. httpx
    removes dot segments (``os/mxwo/../../script/X`` is sent as ``script/X``)
    and a raw ``?`` or ``#`` starts the query, so either could address a
    different Maximo endpoint than the resource being patched. Tests that
    expect a refusal register no response, so any request would fail them."""

    @pytest.mark.parametrize(
        "resource_type",
        [
            "",
            ".",
            "..",
            "mxwo/..",
            "../script",
            "../../../api",
            "mxwo/_QkVERk9SRC9XQzY-",
            "mxwo?_action=x",
            "mxwo#x",
            "mxwo%2F..",
            "mxwo;x",
            "mxwo\n",
            "\N{FULLWIDTH LATIN SMALL LETTER M}xwo",  # a letter, but not ASCII
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_object_structure_refused_before_any_request(
        self, httpx_mock, resource_type: str
    ) -> None:
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type=resource_type,
            resource_id="WO-1",
            attributes={"status": "COMP"},
        )
        assert result == {"error": _OBJECT_STRUCTURE_REFUSAL}
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize(
        "resource_id",
        [
            # As-is, these PATCH the mxwo collection or the object-structure root.
            "",
            ".",
            "..",
            # As-is, httpx resolves the dot segments into another endpoint; once
            # encoded, a proxy that decodes %2F before routing would.
            "../../script/X",
            "x/../../../somethingelse",
            "..\\mxasset",
            "a/..;/b",
            "/",
            "a//b",
        ],
    )
    @pytest.mark.asyncio
    async def test_id_that_could_leave_its_segment_refused_before_any_request(
        self, httpx_mock, resource_id: str
    ) -> None:
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type="mxwo",
            resource_id=resource_id,
            attributes={"status": "COMP"},
        )
        assert result == {"error": _RECORD_ID_REFUSAL}
        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    async def test_id_with_a_lone_surrogate_refused_before_any_request(self, httpx_mock) -> None:
        """JSON "\\ud800" survives the Streamable HTTP transport's ``json.loads``;
        the UTF-8 codec's own error would quote the character back."""
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type="mxwo",
            resource_id="WO-\ud800",
            attributes={"status": "COMP"},
        )
        assert result == {
            "error": "Invalid record ID: an ID must be text that can be sent as UTF-8"
        }
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize(
        ("resource_type", "resource_id", "refusal"),
        [
            ("../../../api", "WO-1", _OBJECT_STRUCTURE_REFUSAL),
            ("mxwo", "../mxasset/A1", _RECORD_ID_REFUSAL),
        ],
    )
    @pytest.mark.asyncio
    async def test_refused_in_sandbox_mode_too(
        self, httpx_mock, resource_type: str, resource_id: str, refusal: str
    ) -> None:
        """A sandbox run answers as a live run would, not "update logged"."""
        try:
            result = await maximo_raw_attribute_update(
                _maximo_ctx(sandbox_mode=True),
                resource_type=resource_type,
                resource_id=resource_id,
                attributes={"status": "COMP"},
            )
        finally:
            set_sandbox_mode(False)
        assert result == {"error": refusal}
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize(
        ("resource_id", "segment"),
        [
            ("X?_action=x#f", "X%3F_action%3Dx%23f"),
            ("x?lean=0", "x%3Flean%3D0"),
            ("BEDFORD/WC6", "BEDFORD%2FWC6"),
            ("WO;1", "WO%3B1"),
            # A server that decodes the path before routing would see "../mxasset".
            ("..%2Fmxasset", "..%252Fmxasset"),
        ],
    )
    @pytest.mark.asyncio
    async def test_id_is_sent_as_one_path_segment(
        self, httpx_mock, resource_id: str, segment: str
    ) -> None:
        path = f"/maximo/oslc/os/mxwo/{segment}?lean=1"
        httpx_mock.add_response(method="POST", url=f"{_MAXIMO_URL}{path}", status_code=204)
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type="mxwo",
            resource_id=resource_id,
            attributes={"status": "COMP"},
        )
        assert result == {"status_code": 204, "body": {}}
        [request] = httpx_mock.get_requests()
        assert request.url.raw_path == path.encode()

    @pytest.mark.parametrize(
        "resource_id",
        [
            "_QkVERk9SRC9XQzY-",  # SITEID/WONUM "BEDFORD/WC6", from IBM's docs
            "_5ZCN5Y_k5bGLLzEwMDA-",  # "名古屋/1000": base64 "+" sent as "_"
            "_6KW~5a6JLzEwMDA-",  # "西安/1000": base64 "/" sent as "~"
        ],
    )
    @pytest.mark.asyncio
    async def test_rest_id_is_sent_unchanged(self, httpx_mock, resource_id: str) -> None:
        """Maximo rest IDs use only unreserved URL characters."""
        path = f"/maximo/oslc/os/mxwo/{resource_id}?lean=1"
        httpx_mock.add_response(method="POST", url=f"{_MAXIMO_URL}{path}", status_code=204)
        result = await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type="mxwo", resource_id=resource_id, attributes={"x": 1}
        )
        assert result == {"status_code": 204, "body": {}}
        [request] = httpx_mock.get_requests()
        assert request.url.raw_path == path.encode()

    @pytest.mark.parametrize("resource_type", ["mxwo", "MXASSET", "ZZ_WO", "mxapiwodetail2"])
    @pytest.mark.asyncio
    async def test_object_structure_name_is_sent_unchanged(
        self, httpx_mock, resource_type: str
    ) -> None:
        """Product and site-defined object structures alike."""
        httpx_mock.add_response(
            method="POST",
            url=f"{_MAXIMO_URL}/maximo/oslc/os/{resource_type}/R1?lean=1",
            status_code=204,
        )
        result = await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type=resource_type, resource_id="R1", attributes={"x": 1}
        )
        assert result == {"status_code": 204, "body": {}}

    @pytest.mark.asyncio
    async def test_attributes_sent_as_a_merge_update_with_connector_auth(self, httpx_mock) -> None:
        """The update form IBM documents: POST with ``x-method-override: PATCH``.
        ``patchtype: MERGE`` keeps the child objects the attributes do not list."""
        httpx_mock.add_response(
            method="POST",
            url=f"{_MAXIMO_URL}/maximo/oslc/os/MXASSET/PUMP_201?lean=1",
            json={"description": "Pump"},
        )
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type="MXASSET",
            resource_id="PUMP_201",
            attributes={"description": "Pump"},
        )
        assert result == {"status_code": 200, "body": {"description": "Pump"}}
        [request] = httpx_mock.get_requests()
        assert request.headers["apikey"] == "k"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["x-method-override"] == "PATCH"
        assert request.headers["patchtype"] == "MERGE"
        assert json.loads(request.content) == {"description": "Pump"}


class TestMaximoRawUpdateHttpFailures:
    """The raw update is a POST and follows POST's retry policy: a 429 is
    retried, because Maximo refused it, but a network error or a 503 is not
    replayed, because the first POST may already have been applied. pytest-httpx answers each
    registered response once, so a replay would find no response."""

    _URL = f"{_MAXIMO_URL}/maximo/oslc/os/mxwo/WO-1?lean=1"

    @pytest.fixture(autouse=True)
    def _no_backoff_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("machina.connectors.cmms.retry.asyncio.sleep", _fake_sleep)

    async def _update(self) -> dict[str, object]:
        return await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type="mxwo", resource_id="WO-1", attributes={"x": 1}
        )

    @pytest.mark.asyncio
    async def test_network_error_is_an_error_entry_without_a_replay(self, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=self._URL)
        result = await self._update()
        assert result == {"error": "maximo_raw_attribute_update failed: ConnectError"}
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_non_json_body_keeps_the_status(self, httpx_mock) -> None:
        """A proxy's error page is not an error entry: the write may have applied."""
        httpx_mock.add_response(
            method="POST", url=self._URL, status_code=502, text="<html>Bad Gateway</html>"
        )
        result = await self._update()
        assert result == {"status_code": 502, "body": {}}

    @pytest.mark.asyncio
    async def test_503_is_reported_without_a_replay(self, httpx_mock) -> None:
        httpx_mock.add_response(method="POST", url=self._URL, status_code=503)
        result = await self._update()
        assert result == {"status_code": 503, "body": {}}
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_429_is_retried(self, httpx_mock) -> None:
        httpx_mock.add_response(method="POST", url=self._URL, status_code=429)
        httpx_mock.add_response(method="POST", url=self._URL, status_code=204)
        result = await self._update()
        assert result == {"status_code": 204, "body": {}}
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    async def test_vendor_error_is_returned_as_sent(self, httpx_mock) -> None:
        body = {"Error": {"reasonCode": "BMXAA4214E", "statusCode": "400"}}
        httpx_mock.add_response(method="POST", url=self._URL, status_code=400, json=body)
        result = await self._update()
        assert result == {"status_code": 400, "body": body}
        assert len(httpx_mock.get_requests()) == 1


class TestVendorWritesGuardedPastTheToolCheck:
    """Each raw vendor write goes through a connector helper with its own
    ``@sandbox_aware`` guard, so a sandbox-mode server sends nothing even when
    the tool's own ``get_sandbox_mode()`` short-circuit does not fire."""

    @pytest.fixture(autouse=True)
    def _tool_check_misses_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("machina.connectors.base.get_sandbox_mode", lambda: False)

    @pytest.mark.asyncio
    async def test_maximo_raw_update_sends_nothing(self, httpx_mock) -> None:
        try:
            result = await maximo_raw_attribute_update(
                _maximo_ctx(sandbox_mode=True),
                resource_type="mxwo",
                resource_id="WO-1",
                attributes={"status": "COMP"},
            )
        finally:
            set_sandbox_mode(False)
        assert result["metadata"]["sandbox"] is True
        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    async def test_sap_raw_notification_sends_nothing(self, httpx_mock) -> None:
        try:
            result = await sap_pm_raw_iw38_notification(
                _sap_ctx(sandbox_mode=True), equipment_id="EQ-1", description="test"
            )
        finally:
            set_sandbox_mode(False)
        assert result["metadata"]["sandbox"] is True
        assert httpx_mock.get_requests() == []


_SAP_NOTIFICATION_URL = f"{_SAP_URL}/API_MAINTENANCENOTIFICATION/MaintenanceNotification"


async def _sap_notify(**ctx_kwargs: bool) -> dict[str, object]:
    return await sap_pm_raw_iw38_notification(
        _sap_ctx(**ctx_kwargs), equipment_id="EQ-1", description="Seal leak"
    )


async def _maximo_patch(**ctx_kwargs: bool) -> dict[str, object]:
    return await maximo_raw_attribute_update(
        _maximo_ctx(**ctx_kwargs), resource_type="mxwo", resource_id="WO-1", attributes={"x": 1}
    )


class TestVendorWriteOutcomes:
    """What a raw vendor write reports back to the MCP client."""

    @pytest.mark.asyncio
    async def test_sap_notification_is_posted_with_the_session_token(self, httpx_mock) -> None:
        httpx_mock.add_response(method="GET", headers={"x-csrf-token": "TOK1"})
        httpx_mock.add_response(
            method="POST", url=_SAP_NOTIFICATION_URL, status_code=201, json={"d": {"N": "1"}}
        )
        result = await _sap_notify()
        assert result == {"status_code": 201, "body": {"d": {"N": "1"}}}
        fetch, post = httpx_mock.get_requests()
        assert fetch.method == "GET"
        assert post.headers["X-CSRF-Token"] == "TOK1"
        assert json.loads(post.content) == {
            "NotificationType": "M2",
            "Equipment": "EQ-1",
            "NotificationText": "Seal leak",
        }

    @pytest.mark.asyncio
    async def test_sap_non_json_body_keeps_the_status(self, httpx_mock) -> None:
        httpx_mock.add_response(method="GET", headers={"x-csrf-token": "TOK1"})
        httpx_mock.add_response(method="POST", url=_SAP_NOTIFICATION_URL, status_code=201)
        assert await _sap_notify() == {"status_code": 201, "body": {}}

    @pytest.mark.asyncio
    async def test_machina_error_keeps_its_message(self, httpx_mock) -> None:
        httpx_mock.add_response(method="GET", status_code=401)
        result = await _sap_notify()
        assert result == {"error": "SAP PM authentication failed during CSRF token fetch"}
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.parametrize(
        ("write", "operation"),
        [
            (_maximo_patch, "maximo_raw_attribute_update"),
            (_sap_notify, "sap_pm_raw_iw38_notification"),
        ],
    )
    @pytest.mark.asyncio
    async def test_foreign_exception_text_never_reaches_the_client(
        self, httpx_mock, write, operation: str
    ) -> None:
        """h11 refuses an API key with a stray newline and quotes it back."""
        httpx_mock.add_exception(httpx.LocalProtocolError("Illegal header value b'TOPSECRET\n'"))
        result = await write()
        assert result == {"error": f"{operation} failed: LocalProtocolError"}
        assert "TOPSECRET" not in str(result)

    @pytest.mark.parametrize("write", [_maximo_patch, _sap_notify])
    @pytest.mark.asyncio
    async def test_disconnected_connector_is_refused_before_any_request(
        self, httpx_mock, write
    ) -> None:
        """A connector whose connect() failed at startup stays registered; the
        raw writes refuse it, as the domain writes do."""
        result = await write(connected=False)
        assert result == {"error": "Not connected — call connect() first"}
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize(
        ("status_code", "level"),
        [
            (200, "info"),
            (204, "info"),
            pytest.param(302, "warning", id="302-redirect-to-a-login-page"),
            (400, "warning"),
            (503, "warning"),
        ],
    )
    @pytest.mark.asyncio
    async def test_maximo_write_is_logged_by_its_status_only(
        self, httpx_mock, status_code: int, level: str
    ) -> None:
        """A write Maximo did not accept is logged at WARNING. The log names the
        connector, the operation and the status: never the body, which can
        repeat record values, nor the request and its credentials."""
        httpx_mock.add_response(
            method="POST",
            url=f"{_MAXIMO_URL}/maximo/oslc/os/mxwo/WO-1?lean=1",
            status_code=status_code,
            json={"Error": {"message": "BMXAA4214E WO-1 SECRET-VALUE"}},
        )
        with structlog.testing.capture_logs() as logs:
            result = await _maximo_patch()
        assert result["status_code"] == status_code
        assert [e for e in logs if e["event"] == "vendor_write_completed"] == [
            {
                "event": "vendor_write_completed",
                "connector": "MaximoConnector",
                "operation": "maximo_raw_attribute_update",
                "status_code": status_code,
                "log_level": level,
            }
        ]
        assert "SECRET-VALUE" not in str(logs)

    @pytest.mark.parametrize(("status_code", "level"), [(201, "info"), (403, "warning")])
    @pytest.mark.asyncio
    async def test_sap_write_is_logged_by_its_status_only(
        self, httpx_mock, status_code: int, level: str
    ) -> None:
        httpx_mock.add_response(method="GET", headers={"x-csrf-token": "TOK1"})
        httpx_mock.add_response(
            method="POST",
            url=_SAP_NOTIFICATION_URL,
            status_code=status_code,
            json={"error": {"message": "SECRET-VALUE"}},
        )
        with structlog.testing.capture_logs() as logs:
            await _sap_notify()
        [completed] = [e for e in logs if e["event"] == "vendor_write_completed"]
        assert completed == {
            "event": "vendor_write_completed",
            "connector": "SapPmConnector",
            "operation": "sap_pm_raw_iw38_notification",
            "status_code": status_code,
            "log_level": level,
        }
        assert "SECRET-VALUE" not in str(logs)
        assert "TOK1" not in str(logs)
