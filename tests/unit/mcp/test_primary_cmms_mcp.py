"""The MCP server sends CMMS calls to the configured primary only.

A config with several CMMS-capable connectors and no ``primary`` is refused
before anything is served; a primary that cannot be built leaves the CMMS
tools registered and answering with the degraded error; and a raw vendor tool
of the primary's vendor reaches the primary, never another connector of that
vendor. The real-client tests go through ``build_server(config)``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, ClassVar

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from machina import runtime as machina_runtime
from machina.config.schema import ConnectorConfig, MachinaConfig
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType
from machina.exceptions import ConnectorConfigError, ConnectorError
from machina.mcp.__main__ import run_serve
from machina.mcp.server import build_http_app, build_server
from machina.mcp.tools_vendor import _find_connector_by_type
from machina.runtime import MachinaRuntime

_SECRET = "s3cr3t-api-key-value"
_DEGRADED = "failed to start"
_TYPE_ONLY = "Asset lookup on the CMMS failed (ConnectorError)"
_NOT_CONNECTED = "Not connected: call connect() first"

_CALLS: list[str] = []


class _Cmms:
    """A CMMS that reads assets and records every call it receives."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_ASSETS, Capability.READ_FAILURE_MODES}
    )

    def __init__(self, **_settings: Any) -> None:
        pass

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def read_assets(self) -> list[Asset]:
        _CALLS.append(f"{type(self).__name__}.read_assets")
        return [Asset(id="P-201", name="Cooling pump", type=AssetType.ROTATING_EQUIPMENT)]

    async def get_asset(self, asset_id: str) -> Asset:
        _CALLS.append(f"{type(self).__name__}.get_asset")
        return (await self.read_assets())[0]

    async def read_failure_modes(self) -> list[Any]:
        return []


class _Secondary(_Cmms):
    pass


class _Unavailable(_Cmms):
    """Builds, but its connection failed: every call says it is not connected."""

    async def connect(self) -> None:
        raise ConnectorError("Failed to connect (ConnectionRefusedError)")

    async def read_assets(self) -> list[Asset]:
        raise ConnectorError(_NOT_CONNECTED)

    async def get_asset(self, asset_id: str) -> Asset:
        raise ConnectorError(_NOT_CONNECTED)


class _Broken:
    """Reads assets (declared per instance, as GenericCmms does), but cannot be built."""

    _BASE_CAPABILITIES: ClassVar[frozenset[Capability]] = frozenset({Capability.READ_ASSETS})
    capabilities = property(lambda self: frozenset({Capability.READ_ASSETS}))

    def __init__(self, **settings: Any) -> None:
        raise ValueError(f"bad credential {settings.get('api_key')}")


class SapPmFirst(_Cmms):
    pass


class SapPmSecond(_Cmms):
    pass


class SapPmBroken(_Broken):
    pass


class MaximoFake(_Cmms):
    pass


class MaximoBroken(_Broken):
    pass


_TYPES = {
    "pmcp_cmms": "_Cmms",
    "pmcp_secondary": "_Secondary",
    "pmcp_unavailable": "_Unavailable",
    "pmcp_broken": "_Broken",
    "pmcp_sap_first": "SapPmFirst",
    "pmcp_sap_second": "SapPmSecond",
    "pmcp_sap_broken": "SapPmBroken",
    "pmcp_maximo": "MaximoFake",
    "pmcp_maximo_broken": "MaximoBroken",
}


@pytest.fixture(autouse=True)
def _fake_types(monkeypatch: pytest.MonkeyPatch) -> None:
    for type_name, cls_name in _TYPES.items():
        monkeypatch.setitem(
            machina_runtime._CONNECTOR_FACTORIES, type_name, f"{__name__}.{cls_name}"
        )
    _CALLS.clear()


def _conn(type_name: str, *, primary: bool = False) -> ConnectorConfig:
    return ConnectorConfig(type=type_name, primary=primary, settings={"api_key": _SECRET})


def _config(**connectors: ConnectorConfig) -> MachinaConfig:
    return MachinaConfig(connectors=connectors)


def _text(result: Any) -> str:
    return "".join(block.text for block in result.content)


# ---------------------------------------------------------------------------
# Refused at startup
# ---------------------------------------------------------------------------


_AMBIGUOUS = {"a": _conn("pmcp_cmms"), "b": _conn("pmcp_secondary")}


def test_build_server_refuses_several_cmms_without_a_primary() -> None:
    with pytest.raises(ConnectorConfigError):
        build_server(_config(**_AMBIGUOUS))


def test_run_serve_prints_one_error_line_and_returns_one(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # serve() sets this variable and configures structlog for the whole
    # process, with loggers cached on first use; both would outlive the test
    # and silence later tests' log capture.
    monkeypatch.setenv("MACHINA_MCP_STDIO", "0")
    monkeypatch.setattr("machina.mcp.server._configure_logging", lambda _config: None)
    path = tmp_path / "machina.yaml"
    path.write_text(
        "connectors:\n  a:\n    type: pmcp_cmms\n  b:\n    type: pmcp_secondary\n",
        encoding="utf-8",
    )

    code = run_serve(
        argparse.Namespace(config=str(path), transport="stdio", host="127.0.0.1", port=8000)
    )

    assert code == 1
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err_lines) == 1
    assert err_lines[0].startswith("Error: invalid configuration:")
    assert "primary: true" in err_lines[0]


# ---------------------------------------------------------------------------
# A degraded primary, over a real MCP client
# ---------------------------------------------------------------------------


def _degraded_config() -> MachinaConfig:
    return _config(
        broken=_conn("pmcp_broken", primary=True),
        other=_conn("pmcp_secondary"),
    )


@pytest.mark.asyncio
async def test_degraded_primary_keeps_the_cmms_tools_and_answers_degraded() -> None:
    async with create_connected_server_and_client_session(
        build_server(_degraded_config())
    ) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        listed = await client.call_tool("machina_list_assets", {})
        got = await client.call_tool("machina_get_asset", {"asset_id": "P-201"})

    assert {"machina_list_assets", "machina_get_asset", "machina_diagnose_failure"} <= names
    listed_text = _text(listed)
    assert _DEGRADED in listed_text
    assert "error" in json.loads(listed_text)
    assert got.isError
    assert _DEGRADED in _text(got)
    assert _SECRET not in listed_text
    assert _SECRET not in _text(got)
    assert _CALLS == []  # the secondary CMMS was never asked


@pytest.mark.asyncio
async def test_a_lone_degraded_primary_keeps_every_cmms_tool_registered() -> None:
    config = _config(broken=_conn("pmcp_broken", primary=True))
    async with create_connected_server_and_client_session(build_server(config)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        got = await client.call_tool("machina_get_asset", {"asset_id": "P-201"})

    assert {
        "machina_list_assets",
        "machina_get_asset",
        "machina_list_work_orders",
        "machina_create_work_order",
        "machina_list_spare_parts",
    } <= names
    assert got.isError
    assert _DEGRADED in _text(got)


@pytest.mark.asyncio
async def test_diagnose_tells_a_degraded_primary_from_an_unavailable_one() -> None:
    args = {"asset_id": "P-201", "symptoms": ["vibration"]}
    async with create_connected_server_and_client_session(
        build_server(_degraded_config())
    ) as client:
        degraded = await client.call_tool("machina_diagnose_failure", args)
    unavailable_config = _config(
        down=_conn("pmcp_unavailable", primary=True), other=_conn("pmcp_secondary")
    )
    async with create_connected_server_and_client_session(
        build_server(unavailable_config)
    ) as client:
        unavailable = await client.call_tool("machina_diagnose_failure", args)

    assert degraded.isError
    assert _DEGRADED in _text(degraded)
    assert _SECRET not in _text(degraded)
    assert not unavailable.isError
    assert _TYPE_ONLY in _text(unavailable)
    assert _DEGRADED not in _text(unavailable)
    assert _CALLS == []


@pytest.mark.asyncio
async def test_unavailable_primary_answers_with_its_own_error_not_the_degraded_text() -> None:
    config = _config(down=_conn("pmcp_unavailable", primary=True), other=_conn("pmcp_secondary"))
    async with create_connected_server_and_client_session(build_server(config)) as client:
        listed = await client.call_tool("machina_list_assets", {})

    text = _text(listed)
    assert _NOT_CONNECTED in text
    assert _DEGRADED not in text
    assert _CALLS == []


# ---------------------------------------------------------------------------
# Raw vendor tools reach the primary of their vendor
# ---------------------------------------------------------------------------


def test_vendor_lookup_returns_the_marked_primary_of_that_vendor() -> None:
    runtime = MachinaRuntime.from_config(
        _config(first=_conn("pmcp_sap_first"), second=_conn("pmcp_sap_second", primary=True))
    )

    assert _find_connector_by_type(runtime, "sappm") is runtime.connectors["second"]


def test_vendor_lookup_never_returns_another_connector_when_the_primary_is_degraded() -> None:
    runtime = MachinaRuntime.from_config(
        _config(
            broken=_conn("pmcp_sap_broken", primary=True),
            other=_conn("pmcp_sap_second"),
        )
    )

    with pytest.raises(ConnectorError, match=_DEGRADED):
        _find_connector_by_type(runtime, "sappm")


def test_vendor_lookup_of_another_vendor_reports_the_degraded_primary() -> None:
    runtime = MachinaRuntime.from_config(
        _config(
            broken=_conn("pmcp_maximo_broken", primary=True),
            sap=_conn("pmcp_sap_first"),
        )
    )

    with pytest.raises(ConnectorError, match=_DEGRADED):
        _find_connector_by_type(runtime, "sappm")


def test_vendor_lookup_never_reaches_a_connector_that_is_not_the_primary() -> None:
    runtime = MachinaRuntime.from_config(
        _config(
            maximo=_conn("pmcp_maximo", primary=True),
            sap=_conn("pmcp_sap_first"),
        )
    )

    with pytest.raises(ConnectorError, match="primary CMMS"):
        _find_connector_by_type(runtime, "sappm")


def test_vendor_lookup_in_a_runtime_built_in_code_keeps_the_first_match() -> None:
    first, second = SapPmFirst(), SapPmSecond()
    runtime = MachinaRuntime(connectors={"a": first, "b": second})

    assert _find_connector_by_type(runtime, "sappm") is first


def test_build_http_app_refuses_several_cmms_without_a_primary() -> None:
    with pytest.raises(ConnectorConfigError):
        build_http_app(_config(**_AMBIGUOUS))
