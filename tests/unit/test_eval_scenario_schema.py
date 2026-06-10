"""CI tests for the eval scenario schema/loader.

Only the schema/loader is CI-tested — the runner itself needs real
Ollama models and is exercised manually (see ``evals/README.md``).
No litellm/Ollama/machina imports anywhere in this module; the loader
is dependency-light by design (stdlib + PyYAML, a core dependency).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evals.conversational.schema import (
    ASSERTION_LAYERS,
    ASSERTION_ORDER,
    LAYER_ORDER,
    LONG_SCENARIO_MIN_TURNS,
    Scenario,
    ScenarioSchemaError,
    load_scenario,
    load_scenarios,
    parse_scenario,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS_DIR = REPO_ROOT / "evals" / "conversational" / "scenarios"


def _valid_data(**overrides: object) -> dict[str, object]:
    """Return a minimal valid scenario dict, with optional overrides."""
    data: dict[str, object] = {
        "id": "test-scenario",
        "description": "A test scenario",
        "turns": [{"user": "hello"}],
    }
    data.update(overrides)
    return data


class TestExampleScenario:
    """The shipped example file parses and exposes the documented shape."""

    def test_example_scenario_parses(self) -> None:
        scenario = load_scenario(SCENARIOS_DIR / "example_smoke.yaml")
        assert scenario.id == "example-smoke"
        assert scenario.connectors is True
        assert scenario.source_path is not None
        assert len(scenario.turns) == 2

        first = scenario.turns[0].assertions
        assert first.expect_retrieval_source == "pump_p201_manual"
        assert first.expect_citation is True
        assert first.golden_contains == ("P-201",)
        # Default: the malformed check is always armed unless disabled.
        assert first.expect_no_malformed is True

        second = scenario.turns[1].assertions
        assert second.expect_tool_invoked == "list_assets"

    def test_all_shipped_scenario_files_validate(self) -> None:
        """Every file in scenarios/ passes the schema (U10 files included)."""
        scenarios = load_scenarios(SCENARIOS_DIR)
        assert scenarios, "scenarios/ must contain at least the example file"
        ids = [s.id for s in scenarios]
        assert len(ids) == len(set(ids)), "scenario ids must be unique"


class TestValidation:
    """Malformed scenarios are rejected with the offending field named."""

    def test_unknown_assertion_key_names_the_field(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "assertions": {"expect_banana": True}}])
        with pytest.raises(ScenarioSchemaError, match="expect_banana"):
            parse_scenario(data)

    def test_unknown_top_level_key_names_the_field(self) -> None:
        with pytest.raises(ScenarioSchemaError, match="golden_answerz"):
            parse_scenario(_valid_data(golden_answerz=["x"]))

    def test_unknown_turn_key_names_the_field(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "asserts": {}}])
        with pytest.raises(ScenarioSchemaError, match="asserts"):
            parse_scenario(data)

    @pytest.mark.parametrize("missing", ["id", "description", "turns"])
    def test_missing_required_field_names_the_field(self, missing: str) -> None:
        data = _valid_data()
        del data[missing]
        with pytest.raises(ScenarioSchemaError, match=missing):
            parse_scenario(data)

    def test_empty_turns_rejected(self) -> None:
        with pytest.raises(ScenarioSchemaError, match="turns"):
            parse_scenario(_valid_data(turns=[]))

    def test_turn_without_user_names_the_field(self) -> None:
        data = _valid_data(turns=[{"assertions": {"expect_citation": True}}])
        with pytest.raises(ScenarioSchemaError, match="user"):
            parse_scenario(data)

    def test_wrong_assertion_type_names_the_field(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "assertions": {"expect_citation": "yes"}}])
        with pytest.raises(ScenarioSchemaError, match="expect_citation"):
            parse_scenario(data)

    def test_golden_contains_must_be_string_list(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "assertions": {"golden_contains": "P-201"}}])
        with pytest.raises(ScenarioSchemaError, match="golden_contains"):
            parse_scenario(data)

    def test_malformed_yaml_file_raises_schema_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("turns: [unclosed", encoding="utf-8")
        with pytest.raises(ScenarioSchemaError, match="YAML"):
            load_scenario(bad)

    def test_unreadable_file_raises_schema_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An OSError while reading is re-raised as ScenarioSchemaError."""

        def _raise_oserror(*args: object, **kwargs: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _raise_oserror)
        with pytest.raises(ScenarioSchemaError, match="cannot read file"):
            load_scenario(SCENARIOS_DIR / "example_smoke.yaml")

    def test_non_mapping_root_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ScenarioSchemaError, match="mapping"):
            load_scenario(bad)

    def test_duplicate_scenario_ids_rejected(self, tmp_path: Path) -> None:
        body = "id: dup\ndescription: d\nturns:\n  - user: hi\n"
        (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(ScenarioSchemaError, match="duplicate scenario id 'dup'"):
            load_scenarios(tmp_path)

    def test_no_files_matched_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ScenarioSchemaError, match="no scenario files"):
            load_scenarios(tmp_path / "nothing-here")


class TestLayerMapping:
    """The assertion-to-layer mapping is total — the attribution contract."""

    def test_every_assertion_type_maps_to_exactly_one_layer(self) -> None:
        # Enumerate the full contract explicitly: a new assertion type
        # MUST be added here AND mapped to exactly one layer.
        expected = {
            "expect_tool_invoked": "runtime",
            "expect_no_malformed": "runtime",
            "expect_retrieval_source": "retrieval",
            "expect_citation": "citations",
            "golden_contains": "golden",
            "golden_excludes": "golden",
        }
        assert expected == ASSERTION_LAYERS

    def test_mapping_covers_the_canonical_order_exactly(self) -> None:
        assert set(ASSERTION_ORDER) == set(ASSERTION_LAYERS)
        assert len(ASSERTION_ORDER) == len(ASSERTION_LAYERS)

    def test_every_layer_value_is_a_known_layer(self) -> None:
        assert set(ASSERTION_LAYERS.values()) == set(LAYER_ORDER)

    def test_canonical_order_is_layer_order(self) -> None:
        """ASSERTION_ORDER is sorted runtime -> retrieval -> citations -> golden."""
        layer_indices = [LAYER_ORDER.index(ASSERTION_LAYERS[name]) for name in ASSERTION_ORDER]
        assert layer_indices == sorted(layer_indices)

    def test_active_assertions_follow_canonical_order(self) -> None:
        scenario = parse_scenario(
            _valid_data(
                turns=[
                    {
                        "user": "hi",
                        "assertions": {
                            "golden_contains": ["x"],
                            "expect_citation": True,
                            "expect_tool_invoked": "list_assets",
                            "expect_retrieval_source": "manual",
                        },
                    }
                ]
            )
        )
        names = [n for n, _ in scenario.turns[0].assertions.active_assertions()]
        assert names == [
            "expect_tool_invoked",
            "expect_no_malformed",
            "expect_retrieval_source",
            "expect_citation",
            "golden_contains",
        ]


class TestConnectorsFlagAndLength:
    """Optional scenario features parse correctly."""

    def test_connectors_false_parses(self) -> None:
        scenario = parse_scenario(_valid_data(connectors=False))
        assert scenario.connectors is False

    def test_connectors_defaults_to_true(self) -> None:
        scenario = parse_scenario(_valid_data())
        assert scenario.connectors is True

    def test_connectors_must_be_boolean(self) -> None:
        with pytest.raises(ScenarioSchemaError, match="connectors"):
            parse_scenario(_valid_data(connectors="no"))

    def test_long_scenario_flag(self) -> None:
        turns = [{"user": f"turn {i}"} for i in range(LONG_SCENARIO_MIN_TURNS)]
        long_scenario = parse_scenario(_valid_data(turns=turns))
        assert long_scenario.is_long is True
        short_scenario = parse_scenario(_valid_data())
        assert isinstance(short_scenario, Scenario)
        assert short_scenario.is_long is False
