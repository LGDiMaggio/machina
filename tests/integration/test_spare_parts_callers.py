"""``read_spare_parts`` called the way Machina's callers call it, on real connectors.

The agent's context prefetch and ``check_spare_parts`` tool, the MCP
``machina_list_spare_parts`` tool and the built-in alarm workflow each have
unit tests against fakes, which cannot show what a real connector does with
the filters they send. These run each caller against the real Maximo
connector, which refuses an asset filter with
``ConnectorUnsupportedFilterError``, and the real UpKeep connector, which
applies it through the asset's ``parts`` list. pytest-httpx mocks the
transport; no real API is called.
"""

from __future__ import annotations

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
from machina.exceptions import ConnectorUnsupportedFilterError
from machina.mcp.tools import machina_list_spare_parts
from machina.runtime import MachinaRuntime
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import Workflow


def _agent(cmms: Any, asset: Asset) -> Agent:
    plant = Plant(name="Test Plant")
    plant.register_asset(asset)
    return Agent(plant=plant, connectors=[cmms])


def _mcp_ctx(cmms: Any) -> Any:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "runtime": MachinaRuntime(connectors={"cmms": cmms}, primary_cmms_name="cmms")
    }
    return ctx


async def _run_spare_parts_step(cmms: Any, asset_id: str) -> Any:
    """Run the alarm workflow's own check_spare_parts step for an alarm on ``asset_id``."""
    step = next(s for s in alarm_to_workorder.steps if s.name == "check_spare_parts")
    registry = ConnectorRegistry()
    registry.register("cmms", cmms)
    result = await WorkflowEngine(registry=registry).execute(
        Workflow(name="Spare parts", steps=[step]), {"asset_id": asset_id}
    )
    (outcome,) = result.step_results
    return outcome


# ---------------------------------------------------------------------------
# Maximo: the asset filter is refused, before any inventory read
# ---------------------------------------------------------------------------

MAXIMO = "https://maximo.example.com"
MAXIMO_PUMP = Asset(id="PUMP-201", name="Cooling Water Pump", type=AssetType.ROTATING_EQUIPMENT)
MAXIMO_REFUSAL = (
    "The Maximo connector cannot filter spare parts by asset: it reads them from "
    "mxinventory, which it cannot query by asset. Look the part up by sku instead."
)
_MXINVENTORY = re.compile(rf"{re.escape(MAXIMO)}/maximo/oslc/os/mxinventory\?")


@pytest.fixture
async def maximo(httpx_mock) -> MaximoConnector:
    httpx_mock.add_response(url=f"{MAXIMO}/maximo/oslc/whoami", json={"userName": "maxadmin"})
    connector = MaximoConnector(url=MAXIMO, auth=ApiKeyHeaderAuth(header_name="apikey", value="k"))
    await connector.connect()
    return connector


def _mock_maximo_part(httpx_mock, sku: str) -> None:
    """One mxinventory member in the shape IBM documents for the selected attributes."""
    httpx_mock.add_response(
        url=_MXINVENTORY,
        json={
            "member": [
                {
                    "itemnum": sku,
                    "siteid": "BEDFORD",
                    "item": {"description": "Bearing SKF 6205"},
                    "invbalances": [{"curbal": 3}],
                }
            ],
            "responseInfo": {},
        },
    )


def _inventory_reads(httpx_mock) -> int:
    return sum(1 for r in httpx_mock.get_requests() if _MXINVENTORY.match(str(r.url)))


class TestMaximoRefusesTheAssetFilter:
    @pytest.mark.asyncio
    async def test_agent_prefetch_says_spare_parts_were_not_checked(
        self, httpx_mock, maximo: MaximoConnector
    ) -> None:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(MAXIMO)}/maximo/oslc/os/mxwo\?"),
            json={"member": [], "responseInfo": {}},
        )  # the work orders are read alongside the spare parts
        agent = _agent(maximo, MAXIMO_PUMP)
        pump = ResolvedEntity(asset=MAXIMO_PUMP, confidence=1.0, match_reason="exact_id")

        context = await agent._gather_context("PUMP-201", [pump])

        assert "spare_parts" not in context
        assert context["spare_parts_unchecked"] == "unsupported"
        assert _inventory_reads(httpx_mock) == 0
        messages = agent._build_messages("PUMP-201", "chat1", context)
        retrieved = next(m["content"] for m in messages if "Retrieved Context" in m["content"])
        assert "**Spare Parts:** not checked: the connected CMMS cannot look up" in retrieved

    @pytest.mark.asyncio
    async def test_agent_tool_returns_the_refusal_to_the_model(
        self, httpx_mock, maximo: MaximoConnector
    ) -> None:
        agent = _agent(maximo, MAXIMO_PUMP)
        result = await agent._execute_tool("check_spare_parts", {"asset_id": "PUMP-201"})
        assert result == {"error": MAXIMO_REFUSAL}
        assert _inventory_reads(httpx_mock) == 0

    @pytest.mark.asyncio
    async def test_agent_tool_looks_up_a_sku(self, httpx_mock, maximo: MaximoConnector) -> None:
        _mock_maximo_part(httpx_mock, "BRG-6205")
        agent = _agent(maximo, MAXIMO_PUMP)
        result = await agent._execute_tool(
            "check_spare_parts", {"asset_id": "", "sku": "BRG-6205"}
        )
        assert [part["sku"] for part in result] == ["BRG-6205"]

    @pytest.mark.asyncio
    async def test_mcp_tool_raises_the_refusal(self, httpx_mock, maximo: MaximoConnector) -> None:
        """FastMCP reports the raised error as an MCP error result."""
        with pytest.raises(ConnectorUnsupportedFilterError) as exc_info:
            await machina_list_spare_parts(_mcp_ctx(maximo), asset_id="PUMP-201")
        assert str(exc_info.value) == MAXIMO_REFUSAL
        assert _inventory_reads(httpx_mock) == 0

    @pytest.mark.asyncio
    async def test_mcp_tool_looks_up_a_sku(self, httpx_mock, maximo: MaximoConnector) -> None:
        _mock_maximo_part(httpx_mock, "BRG-6205")
        result = await machina_list_spare_parts(_mcp_ctx(maximo), sku="BRG-6205")
        assert [part["sku"] for part in result] == ["BRG-6205"]
        assert result[0]["stock_quantity"] == 3

    @pytest.mark.asyncio
    async def test_alarm_workflow_step_is_skipped_with_the_refusal(
        self, httpx_mock, maximo: MaximoConnector
    ) -> None:
        """The step's SKIP policy keeps the refusal on its result."""
        outcome = await _run_spare_parts_step(maximo, "PUMP-201")
        assert outcome.skipped is True
        assert MAXIMO_REFUSAL in outcome.error
        assert _inventory_reads(httpx_mock) == 0


# ---------------------------------------------------------------------------
# UpKeep: the asset filter is applied through the asset's parts list
# ---------------------------------------------------------------------------

UPKEEP = "https://api.onupkeep.com"
UPKEEP_ASSET_ID = "fjQXLsOvG7"
UPKEEP_PUMP = Asset(
    id=UPKEEP_ASSET_ID, name="The Ultra Power Generator", type=AssetType.ROTATING_EQUIPMENT
)
_LISTED = ["ydOS2PdR3a", "Qm7Tz2XbLp"]


@pytest.fixture
async def upkeep(httpx_mock) -> UpKeepConnector:
    httpx_mock.add_response(url=f"{UPKEEP}/api/v2/users?limit=1", json={"results": []})
    connector = UpKeepConnector(api_key="k")
    await connector.connect()
    return connector


def _mock_upkeep_asset_parts(httpx_mock) -> None:
    """The asset lists two parts; each is read on its own (UpKeep's documented shapes)."""
    httpx_mock.add_response(
        url=f"{UPKEEP}/api/v2/assets/{UPKEEP_ASSET_ID}",
        json={"success": True, "result": {"id": UPKEEP_ASSET_ID, "parts": _LISTED}},
    )
    for part_id, name in zip(_LISTED, ["Flux Capacitor", "Oil filter"], strict=True):
        httpx_mock.add_response(
            url=f"{UPKEEP}/api/v2/parts/{part_id}",
            json={"success": True, "result": {"id": part_id, "name": name, "quantity": "5"}},
        )


def _parts_inventory_reads(httpx_mock) -> int:
    return sum(1 for r in httpx_mock.get_requests() if r.url.path == "/api/v2/parts")


class TestUpKeepAppliesTheAssetFilter:
    @pytest.mark.asyncio
    async def test_agent_prefetch_reads_the_assets_parts(
        self, httpx_mock, upkeep: UpKeepConnector
    ) -> None:
        httpx_mock.add_response(
            url=re.compile(rf"{re.escape(UPKEEP)}/api/v2/work-orders\?"), json={"results": []}
        )  # the work orders are read alongside the spare parts
        _mock_upkeep_asset_parts(httpx_mock)
        agent = _agent(upkeep, UPKEEP_PUMP)
        pump = ResolvedEntity(asset=UPKEEP_PUMP, confidence=1.0, match_reason="exact_id")

        context = await agent._gather_context(UPKEEP_ASSET_ID, [pump])

        assert [part.sku for part in context["spare_parts"]] == _LISTED
        assert "spare_parts_unchecked" not in context
        assert _parts_inventory_reads(httpx_mock) == 0  # the inventory is not paged

    @pytest.mark.asyncio
    async def test_agent_tool_reads_the_assets_parts(
        self, httpx_mock, upkeep: UpKeepConnector
    ) -> None:
        _mock_upkeep_asset_parts(httpx_mock)
        agent = _agent(upkeep, UPKEEP_PUMP)
        result = await agent._execute_tool("check_spare_parts", {"asset_id": UPKEEP_ASSET_ID})
        assert [part["sku"] for part in result] == _LISTED

    @pytest.mark.asyncio
    async def test_mcp_tool_applies_both_filters(
        self, httpx_mock, upkeep: UpKeepConnector
    ) -> None:
        _mock_upkeep_asset_parts(httpx_mock)
        result = await machina_list_spare_parts(
            _mcp_ctx(upkeep), asset_id=UPKEEP_ASSET_ID, sku="Qm7Tz2XbLp"
        )
        assert [part["sku"] for part in result] == ["Qm7Tz2XbLp"]
        assert result[0]["name"] == "Oil filter"

    @pytest.mark.asyncio
    async def test_alarm_workflow_step_reads_the_assets_parts(
        self, httpx_mock, upkeep: UpKeepConnector
    ) -> None:
        _mock_upkeep_asset_parts(httpx_mock)
        outcome = await _run_spare_parts_step(upkeep, UPKEEP_ASSET_ID)
        assert outcome.skipped is False
        assert [part.sku for part in outcome.output] == _LISTED
