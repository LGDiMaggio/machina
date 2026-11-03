"""The agent and MCP work-order tools filter on Machina's status values.

The agent's ``read_work_orders`` tool and the MCP ``machina_list_work_orders``
tool hand the ``status`` filter to the connector as given, so a value such as
``"in_progress"`` must reach a vendor CMMS as that vendor's code. All HTTP
traffic is intercepted by pytest-httpx — no real Maximo API calls.
"""

from __future__ import annotations

from importlib.util import find_spec
from unittest.mock import MagicMock

import httpx
import pytest

from machina.agent.runtime import Agent
from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.mappers.maximo import WORK_ORDER_SELECT
from machina.connectors.cmms.maximo import MaximoConnector
from machina.connectors.cmms.upkeep import UpKeepConnector
from machina.exceptions import ConnectorSchemaError
from machina.runtime import MachinaRuntime

BASE = "https://maximo.example.com"
OSLC = f"{BASE}/maximo/oslc"

# INPRG is Maximo's code for Machina's ``in_progress``.
IN_PROGRESS_QUERY = httpx.URL(
    f"{OSLC}/os/mxwo",
    params={
        "lean": "1",
        "oslc.pageSize": "100",
        "oslc.where": 'status="INPRG"',
        "oslc.select": WORK_ORDER_SELECT,
    },
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


# A skip mark, not importorskip in the body: skipping after the fixture has
# registered its mock fails pytest-httpx's unused-response check at teardown.
@pytest.mark.skipif(
    find_spec("mcp") is None, reason="MCP SDK not installed (pip install machina-ai[mcp])"
)
@pytest.mark.asyncio
async def test_mcp_list_work_orders_sends_maximo_code(maximo: MaximoConnector) -> None:
    from machina.mcp.tools import machina_list_work_orders

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"runtime": MachinaRuntime(connectors={"cmms": maximo})}
    result = await machina_list_work_orders(ctx, status="in_progress")
    assert [(wo["id"], wo["status"]) for wo in result] == [("WO-7", "in_progress")]


UPKEEP = "https://api.onupkeep.com"


@pytest.fixture
async def upkeep_on_hold(httpx_mock) -> UpKeepConnector:
    """A connected UpKeep connector whose asset has one work order on hold, assigned."""
    connector = UpKeepConnector(api_key="test-token")
    httpx_mock.add_response(
        method="GET", url=f"{UPKEEP}/api/v2/users?limit=1", json={"results": [{"id": "u1"}]}
    )
    await connector.connect()
    httpx_mock.add_response(
        method="GET",
        url=f"{UPKEEP}/api/v2/work-orders?limit=100&offset=0&asset=a1",
        json={
            "results": [
                {"id": "wo1", "title": "Replace seal", "status": "inProgress", "assetId": "a1"},
                {
                    "id": "wo2",
                    "title": "Awaiting parts",
                    "status": "onHold",
                    "assetId": "a1",
                    "assignedToId": "tech-1",
                },
            ]
        },
    )
    return connector


@pytest.mark.asyncio
async def test_agent_never_sees_an_on_hold_upkeep_work_order_as_assigned(
    upkeep_on_hold: UpKeepConnector,
) -> None:
    """It used to read as ASSIGNED. No status describes it, so the read is refused
    with a reason the model can act on, rather than answered with a wrong one."""
    agent = Agent(connectors=[upkeep_on_hold])
    result = await agent._execute_tool("read_work_orders", {"asset_id": "a1"})
    # The model gets the refusal as the tool's error, not a list of work orders.
    assert isinstance(result, dict)
    assert "1 on hold (onHold)" in result["error"]
    assert "Read the others by status" in result["error"]
    assert "assigned" not in str(result)


@pytest.mark.skipif(
    find_spec("mcp") is None, reason="MCP SDK not installed (pip install machina-ai[mcp])"
)
@pytest.mark.asyncio
async def test_mcp_never_lists_an_on_hold_upkeep_work_order_as_assigned(
    upkeep_on_hold: UpKeepConnector,
) -> None:
    from machina.mcp.tools import machina_list_work_orders

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "runtime": MachinaRuntime(connectors={"cmms": upkeep_on_hold})
    }
    with pytest.raises(ConnectorSchemaError, match="no Machina status describes"):
        await machina_list_work_orders(ctx, asset_id="a1")
