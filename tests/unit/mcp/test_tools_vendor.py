"""Tests for vendor-specific MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
import structlog.testing

from machina.connectors.base import set_sandbox_mode
from machina.connectors.cmms._http_errors import WRITE_OUTCOME_UNKNOWN
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
            "mxwo/_SElMTFRPUC9XNzc-",
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
            ("HILLTOP/W77", "HILLTOP%2FW77"),
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
            "_SElMTFRPUC9XNzc-",  # SITEID/WONUM "HILLTOP/W77", a test value: "=" sent as "-"
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
    async def test_failure_before_sending_says_the_write_request_was_not_sent(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=self._URL)
        result = await self._update()
        assert result == {
            "error": "Maximo update resource attributes failed: ConnectError. "
            "The write request was not sent to the CMMS."
        }
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.parametrize(
        ("exc", "what_happened"),
        [
            pytest.param(httpx.ReadTimeout(""), "timed out after sending began", id="timeout"),
            pytest.param(
                httpx.ReadError(""),
                "was interrupted after sending began: ReadError",
                id="read-error",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_failure_after_sending_is_not_replayed_and_may_have_applied(
        self, httpx_mock, exc: httpx.HTTPError, what_happened: str
    ) -> None:
        httpx_mock.add_exception(exc, url=self._URL)
        result = await self._update()
        assert result == {
            "error": f"Maximo update resource attributes {what_happened}. {WRITE_OUTCOME_UNKNOWN}"
        }
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 500, 502, 503, 504])
    @pytest.mark.asyncio
    async def test_redirect_or_5xx_is_not_replayed_and_may_have_applied(
        self, httpx_mock, status_code: int
    ) -> None:
        """A redirect does not mean the update was not applied (303 See Other
        can answer an applied write), and neither does a 5xx. The vendor's
        body (a proxy's page, an error with internal URLs) is not returned."""
        httpx_mock.add_response(
            method="POST",
            url=self._URL,
            status_code=status_code,
            text="<html>Bad Gateway from maximo-internal:9080</html>",
        )
        result = await self._update()
        assert result == {
            "status_code": status_code,
            "error": f"Maximo update resource attributes was answered with HTTP "
            f"{status_code}. {WRITE_OUTCOME_UNKNOWN}",
        }
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_429_is_retried(self, httpx_mock) -> None:
        httpx_mock.add_response(method="POST", url=self._URL, status_code=429)
        httpx_mock.add_response(method="POST", url=self._URL, status_code=204)
        result = await self._update()
        assert result == {"status_code": 204, "body": {}}
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.parametrize("status_code", [400, 403, 404, 409])
    @pytest.mark.asyncio
    async def test_vendor_refusal_is_an_error_without_the_body(
        self, httpx_mock, status_code: int
    ) -> None:
        body = {
            "Error": {
                "reasonCode": "BMXAA4214E",
                "message": "WO-1 SECRET-VALUE",
                "extendedError": {"moreInfo": {"href": "http://maximo-internal:9080/x"}},
            }
        }
        httpx_mock.add_response(method="POST", url=self._URL, status_code=status_code, json=body)
        result = await self._update()
        assert result == {
            "status_code": status_code,
            "error": f"Maximo update resource attributes failed: HTTP {status_code}",
        }
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_success_with_a_body_that_is_not_json_may_have_applied(self, httpx_mock) -> None:
        """A proxy's sign-in page can answer 200 without the update reaching Maximo."""
        httpx_mock.add_response(
            method="POST", url=self._URL, status_code=200, text="<html>Sign in</html>"
        )
        result = await self._update()
        assert result == {
            "status_code": 200,
            "error": "Maximo update resource attributes was answered with a success status, "
            f"but the response is not JSON. {WRITE_OUTCOME_UNKNOWN}",
        }

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
    @pytest.mark.parametrize("sandbox_mode", [False, True], ids=["live", "sandbox"])
    @pytest.mark.asyncio
    async def test_attributes_without_a_json_form_are_refused_before_any_request(
        self, httpx_mock, value: float, sandbox_mode: bool
    ) -> None:
        """httpx would refuse them only while building the request, which the
        tool could not tell from a failure after sending."""
        try:
            result = await maximo_raw_attribute_update(
                _maximo_ctx(sandbox_mode=sandbox_mode),
                resource_type="mxwo",
                resource_id="WO-1",
                attributes={"estdur": value},
            )
        finally:
            set_sandbox_mode(False)
        assert result == {
            "error": "Invalid attributes: every value must have a JSON form (NaN and infinity "
            "have none), so the update was not sent"
        }
        assert httpx_mock.get_requests() == []

    def test_description_does_not_call_a_redirect_not_applied(self) -> None:
        doc = " ".join((maximo_raw_attribute_update.__doc__ or "").split())
        assert "redirect means it was not applied" not in doc
        assert "or a redirect means" not in doc
        assert "a redirect or another 3xx, a 5xx" in doc
        assert "it may have been applied: read the resource before trying again" in doc


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
        assert result == {
            "error": "SAP PM authentication failed during CSRF token fetch. "
            "The write request was not sent to the CMMS."
        }
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.parametrize(
        ("write", "error"),
        [
            pytest.param(
                _maximo_patch,
                "Maximo update resource attributes was interrupted after sending began: "
                f"LocalProtocolError. {WRITE_OUTCOME_UNKNOWN}",
                id="maximo",
            ),
            pytest.param(
                _sap_notify,
                # The token fetch, which goes first, is refused.
                "SAP PM CSRF token fetch failed: LocalProtocolError. "
                "The write request was not sent to the CMMS.",
                id="sap",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_foreign_exception_text_never_reaches_the_client(
        self, httpx_mock, write, error: str
    ) -> None:
        """h11 refuses an API key with a stray newline and quotes it back."""
        httpx_mock.add_exception(httpx.LocalProtocolError("Illegal header value b'TOPSECRET\n'"))
        result = await write()
        assert result == {"error": error}
        assert "TOPSECRET" not in str(result)

    @pytest.mark.parametrize(
        ("write", "helper", "vendor", "action"),
        [
            pytest.param(
                _maximo_patch,
                "_patch_resource",
                "Maximo",
                "update resource attributes",
                id="maximo",
            ),
            pytest.param(
                _sap_notify,
                "_write_with_csrf",
                "SAP PM",
                "create maintenance notification",
                id="sap",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unexpected_exception_leaves_the_outcome_unknown(
        self, monkeypatch: pytest.MonkeyPatch, write, helper: str, vendor: str, action: str
    ) -> None:
        """Where an unmapped exception was raised, before or after the request
        went out, is not known: the write may have been applied."""

        async def _raise(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("TOPSECRET https://maximo-internal:9080/")

        cls = MaximoConnector if vendor == "Maximo" else SapPmConnector
        monkeypatch.setattr(cls, helper, _raise)
        with structlog.testing.capture_logs() as logs:
            result = await write()
        assert result == {
            "error": f"{vendor} {action} raised RuntimeError. {WRITE_OUTCOME_UNKNOWN}"
        }
        assert "TOPSECRET" not in repr(logs)
        assert "rest_write_outcome_unknown" in [e["event"] for e in logs]

    @pytest.mark.parametrize(
        ("exc", "error"),
        [
            pytest.param(
                httpx.ConnectError(""),
                "SAP PM create maintenance notification failed: ConnectError. "
                "The write request was not sent to the CMMS.",
                id="not-sent",
            ),
            pytest.param(
                httpx.ReadTimeout(f"timed out on {_SAP_NOTIFICATION_URL}"),
                "SAP PM create maintenance notification timed out after sending began. "
                f"{WRITE_OUTCOME_UNKNOWN}",
                id="timeout",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_sap_transport_failure_says_whether_the_notification_was_sent(
        self, httpx_mock, exc: httpx.HTTPError, error: str
    ) -> None:
        httpx_mock.add_response(method="GET", headers={"x-csrf-token": "TOK1"})
        httpx_mock.add_exception(exc, method="POST", url=_SAP_NOTIFICATION_URL)
        assert await _sap_notify() == {"error": error}
        assert len(httpx_mock.get_requests(method="POST")) == 1

    @pytest.mark.parametrize(
        ("status_code", "error"),
        [
            pytest.param(
                400,
                "SAP PM create maintenance notification failed: HTTP 400",
                id="refused",
            ),
            pytest.param(
                303,
                "SAP PM create maintenance notification was answered with HTTP 303. "
                f"{WRITE_OUTCOME_UNKNOWN}",
                id="redirect",
            ),
            pytest.param(
                500,
                "SAP PM create maintenance notification was answered with HTTP 500. "
                f"{WRITE_OUTCOME_UNKNOWN}",
                id="5xx",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_sap_answer_other_than_2xx_is_an_error_without_the_body(
        self, httpx_mock, status_code: int, error: str
    ) -> None:
        httpx_mock.add_response(method="GET", headers={"x-csrf-token": "TOK1"})
        httpx_mock.add_response(
            method="POST",
            url=_SAP_NOTIFICATION_URL,
            status_code=status_code,
            json={"error": {"message": {"value": "SECRET-VALUE at sapgw01.corp"}}},
        )
        assert await _sap_notify() == {"status_code": status_code, "error": error}

    def test_sap_description_says_when_the_notification_may_exist(self) -> None:
        doc = " ".join((sap_pm_raw_iw38_notification.__doc__ or "").split())
        assert "the notification may have been created: check SAP before trying again" in doc

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
