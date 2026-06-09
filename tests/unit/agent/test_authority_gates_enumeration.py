"""U1 — Cross-entity enumeration tool (asset enumeration).

The agent must be able to enumerate the *complete* asset registry rather than
reconstructing it from work orders (which omits assets that have none). See
docs/plans/2026-06-09-001-feat-output-authority-gates-and-v03-hardening-plan.md.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina.agent.runtime import _ENUM_SUMMARY_THRESHOLD, Agent
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant
from machina.llm.tools import MUTATING_TOOLS


def _asset(asset_id: str, *, criticality: Criticality = Criticality.B) -> Asset:
    return Asset(
        id=asset_id,
        name=f"Asset {asset_id}",
        type=AssetType.ROTATING_EQUIPMENT,
        location="Building A",
        criticality=criticality,
    )


def _plant_with(*asset_ids: str) -> Plant:
    plant = Plant(name="Test Plant")
    for aid in asset_ids:
        plant.register_asset(_asset(aid))
    return plant


class _ReadAssetsConnector:
    """Connector stub that only declares the READ_ASSETS capability."""

    capabilities: ClassVar[list[str]] = ["read_assets"]

    async def connect(self) -> None:  # pragma: no cover - trivial
        pass

    async def disconnect(self) -> None:  # pragma: no cover - trivial
        pass

    async def health_check(self) -> bool:  # pragma: no cover - trivial
        return True

    async def read_assets(self, **kwargs: Any) -> list[Asset]:
        return []


class TestListAssetsTool:
    """The list_assets tool returns the full registry, not a WO reconstruction."""

    @pytest.mark.asyncio
    async def test_returns_every_asset_including_wo_less(self) -> None:
        # Six assets; two of them ("P-202", "MOT-201A") would carry no work
        # order in the sample data — they must still appear.
        plant = _plant_with("P-201", "P-202", "COMP-301", "CONV-101", "HX-401", "MOT-201A")
        agent = Agent(plant=plant)

        result = await agent._execute_tool("list_assets", {})

        assert isinstance(result, list)
        ids = {row["id"] for row in result}
        assert ids == {"P-201", "P-202", "COMP-301", "CONV-101", "HX-401", "MOT-201A"}
        assert "MOT-201A" in ids and "P-202" in ids

    @pytest.mark.asyncio
    async def test_empty_registry_returns_empty_list_not_error(self) -> None:
        agent = Agent(plant=Plant(name="Empty Plant"))
        result = await agent._execute_tool("list_assets", {})
        assert result == []

    @pytest.mark.asyncio
    async def test_large_registry_returns_bounded_summary(self) -> None:
        ids = [f"A-{i:03d}" for i in range(_ENUM_SUMMARY_THRESHOLD + 1)]
        agent = Agent(plant=_plant_with(*ids))

        result = await agent._execute_tool("list_assets", {})

        assert isinstance(result, dict)
        assert result["total"] == _ENUM_SUMMARY_THRESHOLD + 1
        assert "by_criticality" in result and "by_type" in result
        # A summary, not an unbounded dump of every record.
        assert "note" in result

    def test_list_assets_is_not_a_write(self) -> None:
        assert "list_assets" not in MUTATING_TOOLS

    def test_offered_when_a_read_assets_connector_is_present(self) -> None:
        agent = Agent(connectors=[_ReadAssetsConnector()])
        names = {tool["function"]["name"] for tool in agent._get_available_tools()}
        assert "list_assets" in names

    def test_not_offered_without_a_read_assets_connector(self) -> None:
        agent = Agent()
        names = {tool["function"]["name"] for tool in agent._get_available_tools()}
        assert "list_assets" not in names
