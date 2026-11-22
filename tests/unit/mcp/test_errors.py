"""What a failed MCP tool call or resource read shows the client (``machina.mcp.errors``).

The real-client tests go through ``build_server(config)``: the server's own
lifespan builds the runtime from the config, and its own registration wraps
the tools and resources. The backend is a fake connector type, registered for
the test, whose calls raise the exception under test.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from mcp.server.fastmcp.tools.base import Tool
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from structlog.testing import capture_logs

from machina import runtime as machina_runtime
from machina.config.schema import ConnectorConfig, MachinaConfig, McpConfig
from machina.connectors.capabilities import Capability
from machina.connectors.cmms._http_errors import WRITE_OUTCOME_UNKNOWN
from machina.domain.asset import Asset, AssetType
from machina.exceptions import (
    ConnectorError,
    ConnectorUnsupportedFilterError,
    DomainValidationError,
)
from machina.mcp import errors as mcp_errors
from machina.mcp.errors import (
    MCP_WRITE_TOOLS,
    MESSAGE_MAY_HAVE_BEEN_SENT,
    guard_resource,
    guard_tool,
    is_guarded,
)
from machina.mcp.server import build_server
from machina.mcp.tools import CAPABILITY_TO_TOOL
from machina.mcp.tools_vendor import VENDOR_TOOLS

if TYPE_CHECKING:
    from collections.abc import Callable

_SECRET = "http://internal-host:8443/x token=abc"
_UNC = r"\\FILESRV01\manuals\pump.pdf"
_NEEDLES = (
    "internal-host",
    "8443",
    "http://",
    "token=",
    "s3cr3t",
    "C:\\Users",
    "FILESRV01",
    "12.000",
    "db-internal",
    "svc_machina",
)

# The four foreign failures every read tool and resource is called under.
_FOREIGN: dict[str, Callable[[], Exception]] = {
    "runtime": lambda: RuntimeError(_SECRET),
    "connect": lambda: httpx.ConnectError(f"All connection attempts failed: {_SECRET}"),
    "vendor_value": lambda: ValueError("invalid literal for int() with base 10: '12.000'"),
    "unc_path": lambda: OSError(2, "No such file or directory", _UNC),
}


def _chained(error: Exception, cause: Exception) -> Exception:
    """*error* as ``raise error from cause`` leaves it."""
    error.__cause__ = cause
    return error


def _unexpected(exc_type: str) -> str:
    return f"Unexpected error ({exc_type}). The details are in the server log."


def _leaked(*texts: object) -> list[str]:
    blob = json.dumps([str(t) for t in texts])
    return [
        needle for needle in _NEEDLES if needle in blob or needle.replace("\\", "\\\\") in blob
    ]


# ---------------------------------------------------------------------------
# A fake backend serving every capability an MCP domain tool needs
# ---------------------------------------------------------------------------


class _Backend:
    """Each method named in ``failing`` raises ``failure()``; the rest answer."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_ASSETS,
            Capability.READ_WORK_ORDERS,
            Capability.GET_WORK_ORDER,
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.CLOSE_WORK_ORDER,
            Capability.CANCEL_WORK_ORDER,
            Capability.READ_SPARE_PARTS,
            Capability.READ_MAINTENANCE_PLANS,
            Capability.READ_MAINTENANCE_HISTORY,
            Capability.READ_FAILURE_MODES,
            Capability.SEARCH_DOCUMENTS,
            Capability.GET_LATEST_READING,
            Capability.SEND_MESSAGE,
        }
    )
    failing: ClassVar[frozenset[str]] = frozenset()
    failure: ClassVar[Callable[[], Exception]] = staticmethod(lambda: RuntimeError(_SECRET))
    calls: ClassVar[list[str]] = []
    connect_failure: ClassVar[Callable[[], Exception] | None] = None

    def __init__(self, **_settings: Any) -> None:
        pass

    async def connect(self) -> None:
        if type(self).connect_failure is not None:
            raise type(self).connect_failure()

    async def disconnect(self) -> None:
        pass

    def _answer(self, method: str, value: Any) -> Any:
        type(self).calls.append(method)
        if method in self.failing:
            raise type(self).failure()
        return value

    async def read_assets(self) -> list[Asset]:
        return self._answer("read_assets", [_asset()])  # type: ignore[no-any-return]

    async def get_asset(self, asset_id: str) -> Asset:
        return self._answer("get_asset", _asset())  # type: ignore[no-any-return]

    async def read_work_orders(self, **_kwargs: Any) -> list[Any]:
        return self._answer("read_work_orders", [])  # type: ignore[no-any-return]

    async def get_work_order(self, work_order_id: str) -> None:
        return self._answer("get_work_order", None)  # type: ignore[no-any-return]

    async def read_spare_parts(self, **_kwargs: Any) -> list[Any]:
        return self._answer("read_spare_parts", [])  # type: ignore[no-any-return]

    async def read_maintenance_plans(self) -> list[Any]:
        return self._answer("read_maintenance_plans", [])  # type: ignore[no-any-return]

    async def read_maintenance_history(self, *, asset_id: str) -> list[Any]:
        return self._answer("read_maintenance_history", [])  # type: ignore[no-any-return]

    async def read_failure_modes(self) -> list[Any]:
        return self._answer("read_failure_modes", [])  # type: ignore[no-any-return]

    async def search_documents(self, query: str, **_kwargs: Any) -> list[Any]:
        return self._answer("search_documents", [])  # type: ignore[no-any-return]

    async def get_latest_reading(self, asset_id: str) -> dict[str, Any]:
        return self._answer("get_latest_reading", {})  # type: ignore[no-any-return]

    async def get_alarms(self, **_kwargs: Any) -> list[Any]:
        return self._answer("get_alarms", [])  # type: ignore[no-any-return]

    async def send_message(self, channel: str, text: str) -> None:
        return self._answer("send_message", None)  # type: ignore[no-any-return]

    async def create_work_order(self, wo: Any) -> Any:
        return self._answer("create_work_order", wo)

    async def update_work_order(self, work_order_id: str, **_kwargs: Any) -> Any:
        return self._answer("update_work_order", None)

    async def close_work_order(self, work_order_id: str) -> Any:
        return self._answer("close_work_order", None)

    async def cancel_work_order(self, work_order_id: str) -> Any:
        return self._answer("cancel_work_order", None)


def _asset() -> Asset:
    return Asset(id="P-201", name="Cooling pump", type=AssetType.ROTATING_EQUIPMENT)


_READ_CALLS: dict[str, dict[str, Any]] = {
    "machina_list_assets": {},
    "machina_get_asset": {"asset_id": "P-201"},
    "machina_list_work_orders": {},
    "machina_get_work_order": {"work_order_id": "WO-1"},
    "machina_list_spare_parts": {"sku": "SKF-6310"},
    "machina_get_maintenance_plan": {},
    "machina_get_maintenance_history": {"asset_id": "P-201"},
    "machina_search_manuals": {"query": "bearing"},
    "machina_get_sensor_reading": {"asset_id": "P-201"},
    "machina_get_alarms": {},
    "machina_diagnose_failure": {"asset_id": "P-201", "symptoms": ["vibration"]},
}

_WRITE_CALLS: dict[str, tuple[dict[str, Any], str]] = {
    "machina_create_work_order": (
        {"asset_id": "P-201", "description": "Replace bearing"},
        "create_work_order",
    ),
    "machina_update_work_order": (
        {"work_order_id": "WO-1", "status": "assigned"},
        "update_work_order",
    ),
    "machina_close_work_order": ({"work_order_id": "WO-1"}, "close_work_order"),
    "machina_cancel_work_order": ({"work_order_id": "WO-1"}, "cancel_work_order"),
    "machina_send_message": ({"channel": "#maint", "text": "P-201 alarm"}, "send_message"),
}

_ALL_METHODS = frozenset(
    {
        "read_assets",
        "get_asset",
        "read_work_orders",
        "get_work_order",
        "read_spare_parts",
        "read_maintenance_plans",
        "read_maintenance_history",
        "read_failure_modes",
        "search_documents",
        "get_latest_reading",
        "get_alarms",
        "send_message",
        "create_work_order",
        "update_work_order",
        "close_work_order",
        "cancel_work_order",
    }
)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> type[_Backend]:
    """Register ``_Backend`` as a connector type and return it for configuring."""
    monkeypatch.setitem(machina_runtime._CONNECTOR_FACTORIES, "sec10_fake", f"{__name__}._Backend")
    monkeypatch.setattr(_Backend, "failing", frozenset())
    monkeypatch.setattr(_Backend, "calls", [])
    monkeypatch.setattr(_Backend, "connect_failure", None)
    return _Backend


def _config(**mcp: Any) -> MachinaConfig:
    return MachinaConfig(
        connectors={"backend": ConnectorConfig(type="sec10_fake", primary=True)},
        mcp=McpConfig(**mcp),
    )


def _fail(
    backend: type[_Backend],
    monkeypatch: pytest.MonkeyPatch,
    failure: Callable[[], Exception],
    *methods: str,
) -> None:
    monkeypatch.setattr(backend, "failing", frozenset(methods))
    monkeypatch.setattr(backend, "failure", staticmethod(failure))


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


async def _tool_raising(exc: BaseException, asset_id: str = "") -> dict[str, Any]:
    """A tool that raises *exc*."""
    raise exc


class TestGuardTool:
    def test_keeps_the_name_docstring_and_signature(self) -> None:
        guarded = guard_tool(_tool_raising)

        assert guarded.__name__ == "_tool_raising"
        assert guarded.__doc__ == "A tool that raises *exc*."
        assert inspect.signature(guarded) == inspect.signature(_tool_raising)
        assert inspect.iscoroutinefunction(guarded)
        assert is_guarded(guarded)
        assert not is_guarded(_tool_raising)

    @pytest.mark.asyncio
    async def test_a_result_passes_through(self) -> None:
        async def tool() -> dict[str, str]:
            return {"status": "ok"}

        assert await guard_tool(tool)() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_a_foreign_exception_is_reported_by_its_type(self) -> None:
        with capture_logs() as logs, pytest.raises(ToolError) as raised:
            await guard_tool(_tool_raising)(RuntimeError(_SECRET))

        assert str(raised.value) == _unexpected("RuntimeError")
        # Nothing downstream can render the original from the chain.
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is True
        (event,) = logs
        assert event["event"] == "mcp_tool_failed"
        assert event["log_level"] == "warning"
        assert event["tool"] == "_tool_raising"
        assert event["error_type"] == "RuntimeError"
        assert event["error"] == _SECRET
        assert "_tool_raising" in event["exception"]
        assert "asset_id" not in event  # tool arguments are never logged

    @pytest.mark.asyncio
    async def test_a_machina_error_keeps_its_message_scrubbed(self) -> None:
        exc = ConnectorError(r"cannot read C:\Users\ops\cmms\export.json")
        with capture_logs() as logs, pytest.raises(ToolError) as raised:
            await guard_tool(_tool_raising)(exc)

        assert str(raised.value) == "cannot read export.json"
        (event,) = logs
        assert "exception" not in event

    @pytest.mark.asyncio
    async def test_an_unsupported_filter_is_logged_at_info(self) -> None:
        with capture_logs() as logs, pytest.raises(ToolError):
            await guard_tool(_tool_raising)(ConnectorUnsupportedFilterError("no asset filter"))

        assert logs[0]["log_level"] == "info"

    @pytest.mark.asyncio
    async def test_cancellation_passes_through(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await guard_tool(_tool_raising)(asyncio.CancelledError())

    @pytest.mark.asyncio
    async def test_a_write_tool_says_the_write_may_have_been_applied(self) -> None:
        with pytest.raises(ToolError) as raised:
            await guard_tool(_tool_raising, write=True)(RuntimeError(_SECRET))

        assert str(raised.value) == f"{_unexpected('RuntimeError')} {WRITE_OUTCOME_UNKNOWN}"

    @pytest.mark.asyncio
    async def test_a_write_tool_does_not_add_it_to_a_machina_error(self) -> None:
        # A Machina error says itself whether the write went out.
        with pytest.raises(ToolError) as raised:
            await guard_tool(_tool_raising, write=True)(ConnectorError("CMMS refused: HTTP 409"))

        assert str(raised.value) == "CMMS refused: HTTP 409"

    @pytest.mark.asyncio
    async def test_no_outcome_sentence_in_sandbox_mode(self) -> None:
        """Every write path is @sandbox_aware, so in sandbox mode no write went out."""
        from machina.connectors.base import set_sandbox_mode

        async def machina_create_work_order() -> None:
            set_sandbox_mode(True)  # as the tool's _runtime(ctx) does
            raise RuntimeError(_SECRET)

        async def call() -> str:
            # In a task of its own, so the mode it sets does not leak out.
            with pytest.raises(ToolError) as raised:
                await guard_tool(machina_create_work_order, write=True)()
            return str(raised.value)

        assert await asyncio.create_task(call()) == _unexpected("RuntimeError")

    @pytest.mark.asyncio
    async def test_the_message_tool_says_the_message_may_have_been_sent(self) -> None:
        async def machina_send_message() -> None:
            raise OSError(_UNC)

        with pytest.raises(ToolError) as raised:
            await guard_tool(machina_send_message, write=True)()

        assert str(raised.value) == f"{_unexpected('OSError')} {MESSAGE_MAY_HAVE_BEEN_SENT}"


class TestGuardResource:
    @pytest.mark.asyncio
    async def test_a_foreign_exception_is_reported_by_its_type(self) -> None:
        async def read_thing(thing_id: str) -> str:
            raise RuntimeError(_SECRET)

        guarded = guard_resource(read_thing)
        with capture_logs() as logs, pytest.raises(ResourceError) as raised:
            await guarded("T-1")

        assert str(raised.value) == _unexpected("RuntimeError")
        assert raised.value.__cause__ is None
        assert logs[0]["event"] == "mcp_resource_failed"
        assert logs[0]["resource"] == "read_thing"
        assert inspect.signature(guarded) == inspect.signature(read_thing)
        assert is_guarded(guarded)

    @pytest.mark.asyncio
    async def test_a_result_passes_through(self) -> None:
        async def read_thing() -> str:
            return "{}"

        assert await guard_resource(read_thing)() == "{}"


# ---------------------------------------------------------------------------
# Registration: every tool guarded, schemas unchanged
# ---------------------------------------------------------------------------


def _all_tool_functions() -> list[Callable[..., Any]]:
    seen: dict[str, Callable[..., Any]] = {}
    for tools in CAPABILITY_TO_TOOL.values():
        for fn in tools:
            seen.setdefault(fn.__name__, fn)
    for fn in VENDOR_TOOLS:
        seen.setdefault(fn.__name__, fn)
    return list(seen.values())


@pytest.mark.parametrize("fn", _all_tool_functions(), ids=lambda fn: fn.__name__)
def test_the_guard_keeps_every_tool_schema(fn: Callable[..., Any]) -> None:
    """FastMCP builds the same tool from the guarded function: input and output
    schemas, the injected Context parameter and the async flag are unchanged
    (the Context and TypedDict traps recorded for mcp 1.27)."""
    plain = Tool.from_function(fn)
    guarded = Tool.from_function(guard_tool(fn, write=fn.__name__ in MCP_WRITE_TOOLS))

    assert guarded.name == plain.name
    assert guarded.description == plain.description
    assert guarded.parameters == plain.parameters
    assert guarded.output_schema == plain.output_schema
    assert guarded.context_kwarg == plain.context_kwarg
    assert guarded.is_async is plain.is_async is True


def test_every_registered_tool_and_cmms_resource_is_guarded(backend: type[_Backend]) -> None:
    server = build_server(_config(enable_vendor_tools=True))

    tools = server._tool_manager.list_tools()
    names = {tool.name for tool in tools}
    assert names >= set(_READ_CALLS) | set(_WRITE_CALLS) | {fn.__name__ for fn in VENDOR_TOOLS}
    assert [tool.name for tool in tools if not is_guarded(tool.fn)] == []
    templates = server._resource_manager.list_templates()
    assert {t.name for t in templates} == {"machina_asset", "machina_work_order"}
    assert [t.name for t in templates if not is_guarded(t.fn)] == []


def test_the_write_tool_list_names_every_write_tool() -> None:
    writes = {
        Capability.CREATE_WORK_ORDER,
        Capability.UPDATE_WORK_ORDER,
        Capability.CLOSE_WORK_ORDER,
        Capability.CANCEL_WORK_ORDER,
        Capability.SEND_MESSAGE,
    }
    expected = {fn.__name__ for cap in writes for fn in CAPABILITY_TO_TOOL[cap]}
    expected |= {fn.__name__ for fn in VENDOR_TOOLS}

    assert expected == MCP_WRITE_TOOLS
    assert set(_WRITE_CALLS) == expected - {fn.__name__ for fn in VENDOR_TOOLS}


@pytest.mark.asyncio
async def test_argument_validation_text_is_unchanged() -> None:
    """Validation runs before the tool body, so the guard never sees it."""
    from machina.mcp.tools import machina_diagnose_failure

    plain, guarded = FastMCP("plain"), FastMCP("guarded")
    plain.add_tool(machina_diagnose_failure)
    guarded.add_tool(guard_tool(machina_diagnose_failure))
    bad = {"asset_id": "P-201", "symptoms": "not a list"}

    with pytest.raises(ToolError) as plain_error:
        await plain.call_tool("machina_diagnose_failure", bad)
    with pytest.raises(ToolError) as guarded_error:
        await guarded.call_tool("machina_diagnose_failure", bad)

    assert str(guarded_error.value) == str(plain_error.value)
    assert "symptoms" in str(guarded_error.value)


# ---------------------------------------------------------------------------
# A real MCP client session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", list(_FOREIGN), ids=list(_FOREIGN))
async def test_no_read_tool_or_resource_shows_foreign_text(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    make = _FOREIGN[failure]
    exc_type = type(make()).__name__
    _fail(backend, monkeypatch, make, *_ALL_METHODS)

    async with create_connected_server_and_client_session(build_server(_config())) as client:
        registered = {tool.name for tool in (await client.list_tools()).tools}
        assert registered - set(_WRITE_CALLS) == set(_READ_CALLS)
        results = {name: await client.call_tool(name, args) for name, args in _READ_CALLS.items()}
        resource_errors = []
        for uri in ("machina://v1/assets/P-201", "machina://v1/work-orders/WO-1"):
            with pytest.raises(McpError) as raised:
                await client.read_resource(uri)  # type: ignore[arg-type]
            resource_errors.append(raised.value.error.message)

    for name, result in results.items():
        assert result.isError is True, name  # still a failure, never a success
        assert result.structuredContent is None, name
        (block,) = result.content
        assert block.text == f"Error executing tool {name}: {_unexpected(exc_type)}"  # type: ignore[union-attr]
        assert _leaked(block) == [], name
    for message in resource_errors:
        assert message.endswith(_unexpected(exc_type))
        assert _leaked(message) == []


@pytest.mark.asyncio
async def test_a_raised_machina_error_keeps_its_message_scrubbed(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail(
        backend,
        monkeypatch,
        lambda: ConnectorError(r"cannot read C:\Users\ops\wo.json"),
        "read_work_orders",
    )

    async with create_connected_server_and_client_session(build_server(_config())) as client:
        result = await client.call_tool("machina_list_work_orders", {})

    assert result.isError is True
    assert (
        result.content[0].text
        == "Error executing tool machina_list_work_orders: cannot read wo.json"
    )  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "method"),
    [
        ("machina_list_assets", {}, "read_assets"),
        ("machina_get_maintenance_history", {"asset_id": "P-201"}, "read_maintenance_history"),
    ],
)
async def test_a_returned_connector_error_is_scrubbed(
    backend: type[_Backend],
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    args: dict[str, Any],
    method: str,
) -> None:
    """These tools return a ConnectorError as an entry instead of raising it."""
    _fail(backend, monkeypatch, lambda: ConnectorError(rf"cannot read {_UNC}"), method)

    async with create_connected_server_and_client_session(build_server(_config())) as client:
        result = await client.call_tool(tool, args)

    assert result.isError is False
    assert result.structuredContent == {"result": [{"error": "cannot read pump.pdf"}]}
    assert _leaked(*result.content) == []


@pytest.mark.asyncio
async def test_a_returned_connector_error_is_logged(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry shows the connector's message; the log names the tool and the
    library error's type (and never its text)."""

    _fail(
        backend,
        monkeypatch,
        lambda: _chained(
            ConnectorError("SQL read failed (OSError)"),
            OSError("server=db-internal.corp;user=svc_machina refused"),
        ),
        "read_assets",
    )

    with capture_logs() as logs:
        async with create_connected_server_and_client_session(build_server(_config())) as client:
            result = await client.call_tool("machina_list_assets", {})

    assert result.structuredContent == {"result": [{"error": "SQL read failed (OSError)"}]}
    (event,) = [e for e in logs if e["event"] == "mcp_tool_failed"]
    assert event["tool"] == "machina_list_assets"
    assert event["cause_type"] == "OSError"
    assert _leaked(event) == []


@pytest.mark.asyncio
async def test_a_connector_that_fails_to_connect_is_logged_by_type_only(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime's connect log names the library error's type, and nothing of
    it reaches the client."""

    def connect_failure() -> Exception:
        return _chained(
            ConnectorError("Failed to connect (ConnectionRefusedError)"),
            ConnectionRefusedError(f"connect to {_SECRET} refused"),
        )

    monkeypatch.setattr(backend, "connect_failure", staticmethod(connect_failure))
    monkeypatch.setattr(
        "machina.runtime.logger", __import__("structlog").get_logger("machina.runtime")
    )

    with capture_logs() as logs:
        async with create_connected_server_and_client_session(build_server(_config())) as client:
            result = await client.call_tool("machina_list_assets", {})

    (event,) = [e for e in logs if e["event"] == "runtime_connector_failed"]
    assert event["connector"] == "backend"
    assert event["error"] == "Failed to connect (ConnectionRefusedError)"
    assert event["cause_type"] == "ConnectionRefusedError"
    assert _leaked(event) == []
    assert _leaked(*result.content, result.structuredContent) == []


@pytest.mark.asyncio
async def test_a_returned_message_error_is_scrubbed(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail(
        backend, monkeypatch, lambda: ConnectorError(rf"relay refused, see {_UNC}"), "send_message"
    )

    async with create_connected_server_and_client_session(build_server(_config())) as client:
        result = await client.call_tool("machina_send_message", {"channel": "#m", "text": "hi"})

    assert result.isError is False
    assert result.structuredContent == {"error": "relay refused, see pump.pdf"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(_WRITE_CALLS), ids=list(_WRITE_CALLS))
async def test_a_write_tool_says_the_outcome_is_unknown(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    args, method = _WRITE_CALLS[tool]
    _fail(backend, monkeypatch, lambda: RuntimeError(_SECRET), method)

    async with create_connected_server_and_client_session(build_server(_config())) as client:
        result = await client.call_tool(tool, args)

    assert method in backend.calls  # the write was attempted, then raised
    assert result.isError is True
    sentence = (
        MESSAGE_MAY_HAVE_BEEN_SENT if tool == "machina_send_message" else WRITE_OUTCOME_UNKNOWN
    )
    assert result.content[0].text == (  # type: ignore[union-attr]
        f"Error executing tool {tool}: {_unexpected('RuntimeError')} {sentence}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            {"work_order_type": "urgent-ish"},
            "Invalid work_order_type. Valid values: corrective, preventive, predictive, improvement",
        ),
        ({"priority": "asap"}, "Invalid priority. Valid values: emergency, high, medium, low"),
    ],
    ids=["type", "priority"],
)
async def test_a_bad_create_argument_names_the_valid_values(
    backend: type[_Backend], args: dict[str, str], message: str
) -> None:
    """Refused before any write, as a Machina error (a DomainValidationError),
    so the client gets a message it can act on, not just a type."""
    call = {"asset_id": "P-201", "description": "Replace bearing", **args}

    with capture_logs() as logs:
        async with create_connected_server_and_client_session(build_server(_config())) as client:
            result = await client.call_tool("machina_create_work_order", call)

    assert result.isError is True
    assert result.content[0].text == f"Error executing tool machina_create_work_order: {message}"  # type: ignore[union-attr]
    assert "create_work_order" not in backend.calls
    (failed,) = [e for e in logs if e["event"] == "mcp_tool_failed"]
    assert failed["error_type"] == DomainValidationError.__name__


@pytest.mark.asyncio
async def test_an_unexpected_catalog_error_is_an_error_not_an_outage(
    backend: type[_Backend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """B9-D4 over the client: a provider bug is ``isError``, never a structured
    "catalog unavailable" answer. (A ConnectorError provider still gives the
    structured partial result: ``test_tools.py`` covers it end to end.)"""
    _fail(backend, monkeypatch, lambda: RuntimeError(_SECRET), "read_failure_modes")

    async with create_connected_server_and_client_session(build_server(_config())) as client:
        result = await client.call_tool(
            "machina_diagnose_failure", {"asset_id": "P-201", "symptoms": ["vibration"]}
        )

    assert result.isError is True
    assert result.structuredContent is None
    text = result.content[0].text  # type: ignore[union-attr]
    assert text == f"Error executing tool machina_diagnose_failure: {_unexpected('RuntimeError')}"
    assert "unavailable" not in text


def test_the_guard_module_needs_no_mcp_sdk_at_import() -> None:
    """``machina.mcp`` imports without the MCP SDK; the guard imports FastMCP's
    error classes only when a call fails."""
    source = inspect.getsource(mcp_errors)
    top_level = source.split("\ndef ", 1)[0]

    assert "from mcp" not in top_level
    assert "import mcp" not in top_level
