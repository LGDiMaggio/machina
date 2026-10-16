"""``read_spare_parts`` called the way its in-tree callers call it.

The agent's context prefetch and ``check_spare_parts`` tool, the MCP
``machina_list_spare_parts`` tool and the builtin alarm-to-work-order workflow
all pass ``asset_id`` (the agent tool passes it even when empty). Their own
tests drive fakes whose ``read_spare_parts`` takes ``**kwargs``, so none of them
could see a connector reject the keyword. These run each caller against the
real Maximo and UpKeep connectors — pytest-httpx mocks the transport, no real
API calls — and check that every connector declaring the capability takes the
callers' keywords.
"""

from __future__ import annotations

import importlib
import inspect
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from machina.agent.entity_resolver import ResolvedEntity
from machina.agent.runtime import Agent
from machina.connectors.base import ConnectorRegistry
from machina.connectors.cmms.auth import ApiKeyHeaderAuth
from machina.connectors.cmms.maximo import MaximoConnector
from machina.connectors.cmms.upkeep import UpKeepConnector
from machina.domain.asset import Asset, AssetType
from machina.domain.plant import Plant
from machina.introspect import describe
from machina.mcp.tools import machina_list_spare_parts
from machina.runtime import MachinaRuntime
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import Workflow

PUMP = Asset(id="P-201", name="Cooling Water Pump", type=AssetType.ROTATING_EQUIPMENT)


def _spare_parts_readers() -> list[type[Any]]:
    """Every registered connector class that declares ``read_spare_parts``."""
    readers = []
    for info in describe().connectors:
        if any(cap.capability == "read_spare_parts" for cap in info.capabilities):
            module_path, class_name = info.dotted_path.rsplit(".", 1)
            readers.append(getattr(importlib.import_module(module_path), class_name))
    return readers


@pytest.mark.parametrize("connector_cls", _spare_parts_readers(), ids=lambda cls: cls.__name__)
def test_read_spare_parts_takes_the_callers_keywords(connector_cls: type[Any]) -> None:
    """Callers find the connector by capability and pass ``asset_id`` / ``sku``."""
    inspect.signature(connector_cls.read_spare_parts).bind(None, asset_id="P-201", sku="BRG-6205")


# ---------------------------------------------------------------------------
# The callers, on the REST connectors that rejected ``asset_id``
# ---------------------------------------------------------------------------


class _Maximo:
    url = "https://maximo.example.com"

    def connector(self, httpx_mock) -> MaximoConnector:
        httpx_mock.add_response(
            url=f"{self.url}/maximo/oslc/whoami", json={"userName": "maxadmin"}
        )
        return MaximoConnector(
            url=self.url, auth=ApiKeyHeaderAuth(header_name="apikey", value="k")
        )

    def mock_work_orders(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(self.url)}/maximo/oslc/os/mxwo\?"), json={"member": []}
        )

    def mock_part(self, httpx_mock, sku: str) -> None:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(self.url)}/maximo/oslc/os/mxinventory\?"),
            json={"member": [{"itemnum": sku, "description": "Bearing", "curbal": 5}]},
        )


class _UpKeep:
    url = "https://api.onupkeep.com"

    def connector(self, httpx_mock) -> UpKeepConnector:
        httpx_mock.add_response(url=f"{self.url}/api/v2/users?limit=1", json={"results": []})
        return UpKeepConnector(api_key="k")

    def mock_work_orders(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(self.url)}/api/v2/work-orders\?"), json={"results": []}
        )

    def mock_part(self, httpx_mock, sku: str) -> None:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(self.url)}/api/v2/parts\?"),
            json={"results": [{"id": "p1", "partNumber": sku, "name": "Bearing", "quantity": 5}]},
        )


@pytest.fixture(params=[_Maximo(), _UpKeep()], ids=["maximo", "upkeep"])
def vendor(request: pytest.FixtureRequest) -> Any:
    return request.param


@pytest.fixture
async def cmms(vendor: Any, httpx_mock) -> Any:
    connector = vendor.connector(httpx_mock)
    await connector.connect()
    return connector


def _agent(cmms: Any) -> Agent:
    plant = Plant(name="Test Plant")
    plant.register_asset(PUMP)
    return Agent(plant=plant, connectors=[cmms])


@pytest.mark.asyncio
async def test_agent_prefetch_reads_spare_parts(vendor: Any, cmms: Any, httpx_mock) -> None:
    """The prefetch gathers with ``return_exceptions=True``: a rejected keyword was
    only logged (``context_gather_error``) and the parts left out of the prompt."""
    vendor.mock_work_orders(httpx_mock)  # read alongside the spare parts
    pump = ResolvedEntity(asset=PUMP, confidence=1.0, match_reason="exact_id")
    context = await _agent(cmms)._gather_context("P-201", [pump])
    assert context.get("spare_parts") == []


@pytest.mark.asyncio
async def test_agent_tool_looks_up_a_sku(vendor: Any, cmms: Any, httpx_mock) -> None:
    """``check_spare_parts`` forwards ``asset_id=""`` with a sku-only lookup too."""
    vendor.mock_part(httpx_mock, "BRG-6205")
    result = await _agent(cmms)._execute_tool("check_spare_parts", {"sku": "BRG-6205"})
    assert [part["sku"] for part in result] == ["BRG-6205"]


@pytest.mark.asyncio
async def test_agent_tool_with_an_asset(cmms: Any) -> None:
    result = await _agent(cmms)._execute_tool("check_spare_parts", {"asset_id": "P-201"})
    assert result == []


@pytest.mark.asyncio
async def test_mcp_tool_with_an_asset(cmms: Any) -> None:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "runtime": MachinaRuntime(connectors={"cmms": cmms}, primary_cmms_name="cmms")
    }
    assert await machina_list_spare_parts(ctx, asset_id="P-201") == []


@pytest.mark.asyncio
async def test_alarm_workflow_spare_parts_step(cmms: Any) -> None:
    """Under ``ErrorPolicy.SKIP`` a rejected keyword skipped the step, and the
    technician notification rendered ``{check_spare_parts}`` literally."""
    step = next(s for s in alarm_to_workorder.steps if s.action == "cmms.read_spare_parts")
    registry = ConnectorRegistry()
    registry.register("cmms", cmms)
    result = await WorkflowEngine(registry=registry).execute(
        Workflow(name="Spare parts", steps=[step]), {"asset_id": "P-201"}
    )
    (outcome,) = result.step_results
    assert (outcome.skipped, outcome.output) == (False, [])
