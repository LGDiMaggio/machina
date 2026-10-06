"""Tests for vendor-specific MCP tools.

HTTP traffic is intercepted by pytest-httpx — no real vendor API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from machina.mcp.tools_vendor import VENDOR_TOOLS, maximo_raw_attribute_update

MAXIMO = "https://maximo.example.com"
OSLC_OS = f"{MAXIMO}/maximo/oslc/os"


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


def _maximo_ctx(*, sandbox_mode: bool = False) -> MagicMock:
    """Build an MCP request context whose runtime holds a Maximo connector."""
    from machina.connectors.cmms.auth import ApiKeyHeaderAuth
    from machina.connectors.cmms.maximo import MaximoConnector
    from machina.runtime import MachinaRuntime

    conn = MaximoConnector(
        url=MAXIMO, auth=ApiKeyHeaderAuth(header_name="apikey", value="test-key")
    )
    runtime = MachinaRuntime(connectors={"maximo": conn}, sandbox_mode=sandbox_mode)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"runtime": runtime}
    return ctx


class TestMaximoRawUpdateReachesOneResource:
    """The PATCH reaches one resource of one object structure, nothing else.

    ``resource_type`` and ``resource_id`` are MCP tool arguments. Interpolated
    into the URL as-is, httpx resolves their dot segments, ``?`` starts a
    query string and ``#`` cuts the URL short. Tests that expect a refusal
    mock nothing, so a request would fail them.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resource_id",
        [
            "_QkVERk9SRC9XQzY-",  # SITEID/WONUM "BEDFORD/WC6", from IBM's docs
            "_5ZCN5Y_k5bGLLzEwMDA-",  # "名古屋/1000": base64 "+" sent as "_"
            "_6KW~5a6JLzEwMDA-",  # "西安/1000": base64 "/" sent as "~"
        ],
    )
    async def test_rest_id_is_sent_unchanged(self, httpx_mock, resource_id: str) -> None:
        """Maximo rest IDs use only unreserved URL characters."""
        httpx_mock.add_response(
            method="PATCH", url=f"{OSLC_OS}/mxwo/{resource_id}", status_code=204
        )
        result = await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type="mxwo", resource_id=resource_id, attributes={"x": 1}
        )
        assert result == {"status_code": 204, "body": {}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("resource_id", "segment"),
        [
            # As-is, httpx resolves the dot segments to PATCH /maximo/oslc/somethingelse.
            ("x/../../../somethingelse", "x%2F..%2F..%2F..%2Fsomethingelse"),
            ("x?lean=0", "x%3Flean%3D0"),
            ("x#y", "x%23y"),
            # A server that decodes the path before routing would see "../mxasset".
            ("..%2Fmxasset", "..%252Fmxasset"),
        ],
    )
    async def test_resource_id_stays_one_path_segment(
        self, httpx_mock, resource_id: str, segment: str
    ) -> None:
        httpx_mock.add_response(method="PATCH", url=f"{OSLC_OS}/mxwo/{segment}", status_code=404)
        result = await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type="mxwo", resource_id=resource_id, attributes={"x": 1}
        )
        assert result == {"status_code": 404, "body": {}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resource_id", ["", ".", ".."])
    async def test_resource_id_cannot_address_a_collection(
        self, httpx_mock, resource_id: str
    ) -> None:
        """As-is, these PATCH the mxwo collection or the object-structure root."""
        result = await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type="mxwo", resource_id=resource_id, attributes={"x": 1}
        )
        assert result == {"error": f"Invalid resource_id {resource_id!r}"}
        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resource_type",
        ["../../../api", "mxwo/_QkVERk9SRC9XQzY-", "mxwo?lean=0", "mxwo#", "mxwo\n", ""],
    )
    async def test_resource_type_must_be_an_object_structure_name(
        self, httpx_mock, resource_type: str
    ) -> None:
        result = await maximo_raw_attribute_update(
            _maximo_ctx(), resource_type=resource_type, resource_id="_QkVERk9SRC9XQzY-"
        )
        assert result["error"].startswith(f"Invalid resource_type {resource_type!r}")
        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    async def test_refused_in_sandbox_mode_too(self) -> None:
        """A sandbox run answers as a live run would, not "update logged"."""
        from machina.connectors.base import set_sandbox_mode

        try:
            result = await maximo_raw_attribute_update(
                _maximo_ctx(sandbox_mode=True), resource_type="../../../api", resource_id="x"
            )
        finally:
            set_sandbox_mode(False)
        assert result["error"].startswith("Invalid resource_type '../../../api'")


class TestVendorToolsSandboxPropagation:
    """A sandbox-mode server must block raw vendor writes even when the
    per-request task did not inherit the sandbox contextvar. Regression:
    the vendor tools read get_sandbox_mode() before _runtime() re-established
    it, and the Maximo raw httpx PATCH has no @sandbox_aware backstop — so the
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
