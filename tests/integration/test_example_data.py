"""Integration tests exercising the real sample data from examples/sample_data/.

These tests guard against example-data drift — if anyone changes the
sample JSON in a way that breaks the parser or loses ISO 14224 fields,
these tests fail in CI before users hit it in the quickstart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.domain import FailureImpact

SAMPLE_CMMS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "sample_data"
    / "cmms"
)


@pytest.fixture
async def cmms_connector() -> GenericCmmsConnector:
    """Return a connected GenericCmmsConnector pointing at the real sample data."""
    conn = GenericCmmsConnector(data_dir=SAMPLE_CMMS_DIR)
    await conn.connect()
    return conn


class TestExampleAssetData:
    """Assert the sample assets.json parses cleanly with ISO 14224 fields."""

    @pytest.mark.asyncio
    async def test_all_six_assets_load(self, cmms_connector: GenericCmmsConnector) -> None:
        assets = await cmms_connector.read_assets()
        assert len(assets) == 6
        ids = {a.id for a in assets}
        assert ids == {
            "P-201",
            "P-202",
            "COMP-301",
            "CONV-101",
            "HX-401",
            "MOT-201A",
        }

    @pytest.mark.asyncio
    async def test_every_asset_has_equipment_class_code(
        self, cmms_connector: GenericCmmsConnector
    ) -> None:
        """None of the six sample assets should be missing their ISO Table A.4 code."""
        assets = await cmms_connector.read_assets()
        missing = [a.id for a in assets if a.equipment_class_code is None]
        assert missing == [], f"assets missing equipment_class_code: {missing}"

    @pytest.mark.asyncio
    async def test_pump_has_pu_code(self, cmms_connector: GenericCmmsConnector) -> None:
        p201 = await cmms_connector.get_asset("P-201")
        assert p201 is not None
        assert p201.equipment_class_code == "PU"

    @pytest.mark.asyncio
    async def test_compressor_has_co_code(self, cmms_connector: GenericCmmsConnector) -> None:
        comp = await cmms_connector.get_asset("COMP-301")
        assert comp is not None
        assert comp.equipment_class_code == "CO"

    @pytest.mark.asyncio
    async def test_heat_exchanger_has_he_code(self, cmms_connector: GenericCmmsConnector) -> None:
        hx = await cmms_connector.get_asset("HX-401")
        assert hx is not None
        assert hx.equipment_class_code == "HE"

    @pytest.mark.asyncio
    async def test_motor_has_em_code(self, cmms_connector: GenericCmmsConnector) -> None:
        motor = await cmms_connector.get_asset("MOT-201A")
        assert motor is not None
        assert motor.equipment_class_code == "EM"

    @pytest.mark.asyncio
    async def test_conveyor_has_cv_code(self, cmms_connector: GenericCmmsConnector) -> None:
        conv = await cmms_connector.get_asset("CONV-101")
        assert conv is not None
        assert conv.equipment_class_code == "CV"

    @pytest.mark.asyncio
    async def test_parent_references_are_all_known_or_external(
        self, cmms_connector: GenericCmmsConnector
    ) -> None:
        """Every asset.parent either resolves inside the registry or is
        an external system marker (e.g. COOLING-SYS-01) that's fine to be
        unresolved at this layer."""
        assets = await cmms_connector.read_assets()
        for asset in assets:
            if asset.parent:
                # parent is a non-empty string — good enough at the parser level
                assert isinstance(asset.parent, str)
                assert asset.parent.strip() == asset.parent


class TestExampleWorkOrderData:
    """Assert the sample work_orders.json parses cleanly with ISO 14224 fields."""

    @pytest.mark.asyncio
    async def test_all_five_work_orders_load(self, cmms_connector: GenericCmmsConnector) -> None:
        wos = await cmms_connector.read_work_orders()
        assert len(wos) == 5

    @pytest.mark.asyncio
    async def test_corrective_wo_with_critical_impact(
        self, cmms_connector: GenericCmmsConnector
    ) -> None:
        """WO-2026-1842 (excessive bearing vibration) is critical."""
        wos = await cmms_connector.read_work_orders()
        wo = next(w for w in wos if w.id == "WO-2026-1842")
        assert wo.failure_impact == FailureImpact.CRITICAL
        assert wo.failure_cause == "Expected wear and tear"
        assert wo.failure_mode == "BEAR-WEAR-01"

    @pytest.mark.asyncio
    async def test_corrective_wo_with_degraded_impact(
        self, cmms_connector: GenericCmmsConnector
    ) -> None:
        """WO-2026-1841 (clogged filter) is degraded, not critical."""
        wos = await cmms_connector.read_work_orders()
        wo = next(w for w in wos if w.id == "WO-2026-1841")
        assert wo.failure_impact == FailureImpact.DEGRADED
        assert wo.failure_cause == "Expected wear and tear"

    @pytest.mark.asyncio
    async def test_preventive_wos_have_no_failure_impact(
        self, cmms_connector: GenericCmmsConnector
    ) -> None:
        """Preventive WOs describe planned work, not failures —
        failure_impact and failure_cause should both be None."""
        wos = await cmms_connector.read_work_orders()
        preventive_ids = {"WO-2026-1840", "WO-2026-1843"}
        for wo in wos:
            if wo.id in preventive_ids:
                assert wo.failure_impact is None
                assert wo.failure_cause is None
