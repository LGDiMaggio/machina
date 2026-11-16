"""Tests for ``machina.agent.diagnosis`` used directly.

Both tool surfaces are covered end to end elsewhere: the agent's
``diagnose_failure`` in ``tests/unit/test_agent_runtime.py`` and the MCP
``machina_diagnose_failure`` in ``tests/unit/mcp/test_tools.py``, including a
parity test between the two. These pin the module's own contract.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from machina.agent.diagnosis import (
    MAX_RESULTS,
    NO_CATALOG_NOTE,
    CatalogHarvest,
    diagnose_symptoms,
    harvest_failure_modes,
)
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType
from machina.domain.failure_mode import FailureMode
from machina.exceptions import ConnectorAuthError, ConnectorError

_PUMP = Asset(id="P-1", name="Pump", type=AssetType.ROTATING_EQUIPMENT)


def _mode(code: str, *indicators: str) -> FailureMode:
    return FailureMode(
        code=code, name=code, category="mechanical", typical_indicators=list(indicators)
    )


class _Provider:
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READ_FAILURE_MODES})

    def __init__(self, result: list[FailureMode] | Exception) -> None:
        self._result = result

    async def read_failure_modes(self) -> list[FailureMode]:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class TestHarvest:
    @pytest.mark.asyncio
    async def test_no_provider_is_an_empty_complete_harvest(self) -> None:
        harvest = await harvest_failure_modes([])
        assert harvest == CatalogHarvest(failure_modes=[], providers=(), failed={})
        assert harvest.partial is False
        assert harvest.unavailable is False
        assert harvest.report() == {"providers": [], "failed_providers": [], "partial": False}

    @pytest.mark.asyncio
    async def test_failed_providers_are_recorded_in_registration_order(self) -> None:
        auth = ConnectorAuthError("401 from https://cmms.example.invalid")
        harvest = await harvest_failure_modes(
            [
                ("a", _Provider(auth)),
                ("b", _Provider([_mode("X-01", "vibration")])),
                ("c", _Provider(ConnectorError("not connected"))),
            ]
        )
        assert [fm.code for fm in harvest.failure_modes] == ["X-01"]
        assert harvest.providers == ("a", "b", "c")
        assert list(harvest.failed) == ["a", "c"]
        assert harvest.failed["a"] is auth
        assert harvest.partial is True
        assert harvest.unavailable is False
        assert harvest.report()["failed_providers"] == [
            {"name": "a", "error_type": "ConnectorAuthError"},
            {"name": "c", "error_type": "ConnectorError"},
        ]
        assert harvest.describe_failed() == (
            "provider 'a' failed (ConnectorAuthError), provider 'c' failed (ConnectorError)"
        )


class TestDiagnoseSymptoms:
    def test_resolved_asset_without_harvest_is_a_caller_bug(self) -> None:
        """Never reported as "no catalog": that would tell the model
        diagnosis is not set up."""
        with pytest.raises(TypeError, match="needs a catalog harvest"):
            diagnose_symptoms("P-1", _PUMP, None, ["vibration"])

    def test_provider_serving_an_empty_catalog_reads_as_not_configured(self) -> None:
        harvest = CatalogHarvest(failure_modes=[], providers=("a",))
        result = diagnose_symptoms("P-1", _PUMP, harvest, ["vibration"])
        assert result["note"] == NO_CATALOG_NOTE
        assert result["catalog"] == {"providers": ["a"], "failed_providers": [], "partial": False}

    def test_match_count_counts_past_the_cut(self) -> None:
        modes = [_mode(f"V-{i}", "vibration_velocity_mm_s") for i in range(MAX_RESULTS + 3)]
        harvest = CatalogHarvest(failure_modes=modes, providers=("a",))
        result = diagnose_symptoms("P-1", _PUMP, harvest, ["vibration"])
        assert len(result["probable_failures"]) == MAX_RESULTS
        assert result["match_count"] == MAX_RESULTS + 3

    def test_ratio_is_never_labelled_a_confidence_or_probability(self) -> None:
        harvest = CatalogHarvest(
            failure_modes=[_mode("V-1", "vibration_velocity_mm_s", "noise_db")], providers=("a",)
        )
        entry = diagnose_symptoms("P-1", _PUMP, harvest, ["vibration"])["probable_failures"][0]
        assert entry["indicator_match_ratio"] == 0.5
        assert not {"confidence", "probability", "likelihood"} & set(entry)
