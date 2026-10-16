"""The agent and MCP work-order tools filter on Machina's status values.

The agent's ``read_work_orders`` tool and the MCP ``machina_list_work_orders``
tool hand the ``status`` filter to the connector as given, so a value such as
``"in_progress"`` must reach a vendor CMMS as that vendor's code. All HTTP
traffic is intercepted by pytest-httpx — no real Maximo API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from machina.agent.runtime import Agent
from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.runtime import MachinaRuntime

BASE = "https://maximo.example.com"
OSLC = f"{BASE}/maximo/oslc"

# INPRG is Maximo's code for Machina's ``in_progress``.
IN_PROGRESS_QUERY = httpx.URL(
    f"{OSLC}/os/mxwo",
    params={"lean": "1", "oslc.pageSize": "100", "oslc.where": 'status="INPRG"'},
)
IN_PROGRESS_WORK_ORDER = {
    "wonum": "WO-7",
    "description": "Replace mechanical seal",
    "wopriority": 2,
    "status": "INPRG",
    "worktype": "CM",
    "assetnum": "PUMP-201",
}


@pytest.fixture
async def maximo(httpx_mock) -> MaximoConnector:
    """A connected Maximo connector that serves one in-progress work order."""
    connector = MaximoConnector(
        url=BASE, auth=ApiKeyHeaderAuth(header_name="apikey", value="test-key")
    )
    httpx_mock.add_response(method="GET", url=f"{OSLC}/whoami", json={"userName": "maxadmin"})
    await connector.connect()
    httpx_mock.add_response(
        method="GET",
        url=IN_PROGRESS_QUERY,
        json={"member": [IN_PROGRESS_WORK_ORDER], "responseInfo": {}},
    )
    return connector


@pytest.mark.asyncio
async def test_agent_read_work_orders_sends_maximo_code(maximo: MaximoConnector) -> None:
    agent = Agent(connectors=[maximo])
    result = await agent._execute_tool("read_work_orders", {"status": "in_progress"})
    assert [(wo["id"], wo["status"]) for wo in result] == [("WO-7", "in_progress")]


@pytest.mark.asyncio
async def test_mcp_list_work_orders_sends_maximo_code(maximo: MaximoConnector) -> None:
    pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")
    from machina.mcp.tools import machina_list_work_orders

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"runtime": MachinaRuntime(connectors={"cmms": maximo})}
    result = await machina_list_work_orders(ctx, status="in_progress")
    assert [(wo["id"], wo["status"]) for wo in result] == [("WO-7", "in_progress")]
