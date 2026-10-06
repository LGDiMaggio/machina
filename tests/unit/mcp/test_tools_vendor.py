"""Tests for vendor-specific MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

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


def _maximo_ctx(*, sandbox_mode: bool = False) -> MagicMock:
    """The context of a server with one connected Maximo connector."""
    conn = MaximoConnector(url=_MAXIMO_URL, auth=ApiKeyHeaderAuth(header_name="apikey", value="k"))
    conn._connected = True
    return _ctx(MachinaRuntime(connectors={"maximo": conn}, sandbox_mode=sandbox_mode))


def _sap_ctx(*, sandbox_mode: bool = False) -> MagicMock:
    """The context of a server with one connected SAP PM connector."""
    conn = SapPmConnector(url=_SAP_URL, auth=BasicAuth(username="u", password="p"))
    conn._connected = True
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


class TestMaximoRawUpdateStaysOnOneResource:
    """``resource_type`` and ``resource_id`` come from the MCP client. httpx
    removes dot segments (``os/mxwo/../../script/X`` is sent as ``script/X``)
    and a raw ``?`` or ``#`` starts the query, so either could address a
    different Maximo endpoint than the resource being patched."""

    @pytest.mark.parametrize(
        "resource_type",
        ["", ".", "..", "mxwo/..", "../script", "mxwo?_action=x", "mxwo#x", "mxwo%2F..", "mxwo\n"],
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
        assert "Invalid Maximo object structure" in result["error"]
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("resource_id", ["", ".", ".."])
    @pytest.mark.asyncio
    async def test_empty_or_dot_segment_id_refused_before_any_request(
        self, httpx_mock, resource_id: str
    ) -> None:
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type="mxwo",
            resource_id=resource_id,
            attributes={"status": "COMP"},
        )
        assert "Invalid record ID" in result["error"]
        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    async def test_id_is_sent_as_one_path_segment(self, httpx_mock) -> None:
        path = "/maximo/oslc/os/mxwo/..%2F..%2Fscript%2FX%3F_action%3Dx%23f"
        httpx_mock.add_response(method="PATCH", url=f"{_MAXIMO_URL}{path}", status_code=204)
        result = await maximo_raw_attribute_update(
            _maximo_ctx(),
            resource_type="mxwo",
            resource_id="../../script/X?_action=x#f",
            attributes={"status": "COMP"},
        )
        assert result == {"status_code": 204, "body": {}}
        [request] = httpx_mock.get_requests()
        assert request.url.raw_path == path.encode()

    @pytest.mark.asyncio
    async def test_attributes_patched_with_connector_auth(self, httpx_mock) -> None:
        httpx_mock.add_response(
            method="PATCH",
            url=f"{_MAXIMO_URL}/maximo/oslc/os/MXASSET/PUMP_201",
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
        assert json.loads(request.content) == {"description": "Pump"}


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
