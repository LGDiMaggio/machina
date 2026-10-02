"""Integration tests for the streamable-http MCP transport, served in-process.

These drive the real Starlette app that :func:`machina.mcp.server.serve`
runs under uvicorn — auth middleware, Host validation, the ``/health`` route
and the process-scoped runtime — through Starlette's ``TestClient``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")

from starlette.testclient import TestClient

import machina
from machina.config.schema import ConnectorConfig, MachinaConfig, McpConfig
from machina.connectors.cmms.generic import GenericCmmsConnector

TOKEN = "t" * 64
SAMPLE_CMMS = Path(__file__).resolve().parents[3] / "examples" / "sample_data" / "cmms"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc(method: str, request_id: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _sse_result(response: Any) -> dict[str, Any]:
    """Return the JSON-RPC message carried by a (possibly SSE-framed) response."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()  # type: ignore[no-any-return]
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())  # type: ignore[no-any-return]
    raise AssertionError(f"no JSON-RPC payload in response: {response.text!r}")


@pytest.fixture()
def lifecycle_counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count GenericCmmsConnector connect/disconnect calls."""
    counts = {"connect": 0, "disconnect": 0}
    original_connect = GenericCmmsConnector.connect
    original_disconnect = GenericCmmsConnector.disconnect

    async def _connect(self: GenericCmmsConnector) -> None:
        counts["connect"] += 1
        await original_connect(self)

    async def _disconnect(self: GenericCmmsConnector) -> None:
        counts["disconnect"] += 1
        await original_disconnect(self)

    monkeypatch.setattr(GenericCmmsConnector, "connect", _connect)
    monkeypatch.setattr(GenericCmmsConnector, "disconnect", _disconnect)
    return counts


@pytest.fixture()
def http_config(monkeypatch: pytest.MonkeyPatch) -> MachinaConfig:
    monkeypatch.setenv("MACHINA_MCP_TOKENS_JSON", json.dumps({TOKEN: "integration-test"}))
    return MachinaConfig(
        sandbox=True,
        connectors={
            "cmms": ConnectorConfig(
                type="generic_cmms",
                primary=True,
                settings={"data_dir": str(SAMPLE_CMMS)},
            )
        },
    )


def _client(config: MachinaConfig, base_url: str = "http://localhost:8000") -> TestClient:
    from machina.mcp.server import build_http_app

    return TestClient(build_http_app(config), base_url=base_url)


class TestHealthRoute:
    def test_unauthenticated_health_is_minimal(self, http_config: MachinaConfig) -> None:
        with _client(http_config) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_authenticated_health_reports_runtime_and_version(
        self, http_config: MachinaConfig
    ) -> None:
        with _client(http_config) as client:
            response = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        body = response.json()
        assert body["status"] == "healthy"
        assert body["connectors"] == ["cmms"]
        assert body["sandbox_mode"] is True
        assert body["version"] == machina.__version__

    def test_invalid_token_gets_minimal_payload(self, http_config: MachinaConfig) -> None:
        with _client(http_config) as client:
            response = client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


class TestMcpEndpoint:
    def test_runtime_connects_once_across_requests(
        self, http_config: MachinaConfig, lifecycle_counts: dict[str, int]
    ) -> None:
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        with _client(http_config) as client:
            init = client.post(
                "/mcp",
                headers=headers,
                json=_rpc(
                    "initialize",
                    1,
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"},
                    },
                ),
            )
            assert init.status_code == 200, init.text
            assert _sse_result(init)["result"]["serverInfo"]["name"] == "machina"

            listed = client.post("/mcp", headers=headers, json=_rpc("tools/list", 2))
            assert listed.status_code == 200, listed.text
            tool_names = {t["name"] for t in _sse_result(listed)["result"]["tools"]}
            assert "machina_list_assets" in tool_names

            called = client.post(
                "/mcp",
                headers=headers,
                json=_rpc("tools/call", 3, {"name": "machina_list_assets", "arguments": {}}),
            )
            assert called.status_code == 200, called.text
            assert "P-201" in json.dumps(_sse_result(called))

            # Three MCP requests, one process-wide runtime: connected exactly once.
            assert lifecycle_counts["connect"] == 1
            assert lifecycle_counts["disconnect"] == 0
        assert lifecycle_counts["disconnect"] == 1

    def test_missing_bearer_is_rejected(self, http_config: MachinaConfig) -> None:
        with _client(http_config) as client:
            response = client.post("/mcp", headers=MCP_HEADERS, json=_rpc("tools/list", 1))
        assert response.status_code == 401

    def test_unlisted_host_is_rejected(self, http_config: MachinaConfig) -> None:
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        with _client(http_config, base_url="http://evil.example") as client:
            response = client.post("/mcp", headers=headers, json=_rpc("tools/list", 1))
        assert response.status_code in (400, 421)

    def test_configured_bare_hostname_is_accepted(self, http_config: MachinaConfig) -> None:
        """A TLS-terminating proxy forwards a Host without a port; listing it works."""
        config = http_config.model_copy(
            update={"mcp": McpConfig(allowed_hosts=["mcp.example.com"])}
        )
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        with _client(config, base_url="http://mcp.example.com") as client:
            response = client.post("/mcp", headers=headers, json=_rpc("tools/list", 1))
        assert response.status_code == 200, response.text
