"""Substrate-agnostic failure-mode harvest (plan success criterion).

The same demo failure-mode catalog loads from a CSV substrate
(``ExcelCsvConnector``) and from the JSON demo substrate
(``GenericCmmsConnector``); both feed the identical capability-gated
harvest and yield equivalent ``diagnose_failure`` behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.docs.excel import ExcelCsvConnector
from machina.connectors.docs.excel_schema import (
    ColumnMapping,
    ExcelConnectorConfig,
    SheetSchema,
)
from machina.domain.asset import Asset, AssetType, Criticality

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DIR = REPO_ROOT / "examples" / "sample_data"

DEMO_CODES = {
    "BEAR-WEAR-01",
    "SEAL-LEAK-01",
    "IMP-EROSION-01",
    "VALVE-FAIL-01",
    "FILTER-CLOG-01",
    "BELT-WEAR-01",
    "MOTOR-OVERHEAT-01",
    "INSULATION-FAIL-01",
    "FOULING-01",
    "GASKET-FAIL-01",
}


def _failure_modes_sheet() -> SheetSchema:
    """Schema for examples/sample_data/failure_modes.csv."""
    return SheetSchema(
        path=str(SAMPLE_DIR / "failure_modes.csv"),
        columns=[
            ColumnMapping(column="code", field="code", required=True),
            ColumnMapping(column="name", field="name", required=True),
            ColumnMapping(column="iso_14224_code", field="iso_14224_code"),
            ColumnMapping(column="mechanism", field="mechanism"),
            ColumnMapping(column="category", field="category"),
            ColumnMapping(column="detection_methods", field="detection_methods"),
            ColumnMapping(column="typical_indicators", field="typical_indicators"),
            ColumnMapping(column="recommended_actions", field="recommended_actions"),
            ColumnMapping(column="mtbf_hours", field="mtbf_hours", type="float"),
        ],
    )


def _demo_pump() -> Asset:
    """Mirror of the demo's P-201 asset and its declared failure codes."""
    return Asset(
        id="P-201",
        name="Cooling Water Pump",
        type=AssetType.ROTATING_EQUIPMENT,
        location="Building A",
        criticality=Criticality.A,
        failure_modes=["BEAR-WEAR-01", "SEAL-LEAK-01", "IMP-EROSION-01"],
    )


def _csv_agent() -> Agent:
    """Agent whose only failure-mode source is the sample CSV."""
    plant = Plant(name="CSV Demo Plant")
    plant.register_asset(_demo_pump())
    connector = ExcelCsvConnector(
        config=ExcelConnectorConfig(failure_modes=_failure_modes_sheet())
    )
    return Agent(name="CSV Agent", plant=plant, connectors=[connector])


def _json_agent() -> Agent:
    """Agent on the demo JSON substrate (Generic CMMS local mode)."""
    return Agent(
        name="JSON Agent",
        plant=Plant(name="JSON Demo Plant"),
        connectors=[GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms")],
    )


class TestCsvSubstrateHarvest:
    @pytest.mark.asyncio
    async def test_csv_substrate_harvests_demo_catalog(self) -> None:
        """The CSV-backed connector contributes all 10 demo modes."""
        agent = _csv_agent()
        await agent.start()
        try:
            catalog = await agent._collect_failure_modes()
            assert {fm.code for fm in catalog} == DEMO_CODES
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_diagnosis_uses_csv_catalog(self) -> None:
        """diagnose_failure answers from the CSV-harvested catalog."""
        agent = _csv_agent()
        await agent.start()
        try:
            result = await agent._execute_tool(
                "diagnose_failure",
                {"asset_id": "P-201", "symptoms": ["high vibration"]},
            )
            codes = [f["code"] for f in result["probable_failures"]]
            assert codes == ["BEAR-WEAR-01", "IMP-EROSION-01"]
            assert "note" not in result
        finally:
            await agent.stop()


class TestSubstrateEquivalence:
    @pytest.mark.asyncio
    async def test_csv_and_json_substrates_yield_same_catalog(self) -> None:
        """Swapping the demo source from JSON to CSV harvests the same codes."""
        csv_agent = _csv_agent()
        json_agent = _json_agent()
        await csv_agent.start()
        await json_agent.start()
        try:
            csv_catalog = {fm.code: fm for fm in await csv_agent._collect_failure_modes()}
            json_catalog = {fm.code: fm for fm in await json_agent._collect_failure_modes()}
            assert set(csv_catalog) == set(json_catalog) == DEMO_CODES
            # Field-level parity spot check — equal code sets alone could
            # hide a substrate that drops list fields or coercions.
            csv_bear = csv_catalog["BEAR-WEAR-01"]
            json_bear = json_catalog["BEAR-WEAR-01"]
            assert csv_bear.typical_indicators == json_bear.typical_indicators
            assert csv_bear.recommended_actions == json_bear.recommended_actions
            assert csv_bear.mtbf_hours == json_bear.mtbf_hours
            assert csv_bear.iso_14224_code == json_bear.iso_14224_code
        finally:
            await csv_agent.stop()
            await json_agent.stop()

    @pytest.mark.asyncio
    async def test_csv_and_json_substrates_yield_same_diagnosis(self) -> None:
        """Identical symptoms produce identical diagnoses on both substrates."""
        csv_agent = _csv_agent()
        json_agent = _json_agent()
        await csv_agent.start()
        await json_agent.start()
        try:
            args = {"asset_id": "P-201", "symptoms": ["high vibration"]}
            csv_result = await csv_agent._execute_tool("diagnose_failure", args)
            json_result = await json_agent._execute_tool("diagnose_failure", args)
            assert csv_result["probable_failures"] == json_result["probable_failures"]
            assert csv_result["probable_failures"]  # non-empty: catalog in play
        finally:
            await csv_agent.stop()
            await json_agent.stop()
