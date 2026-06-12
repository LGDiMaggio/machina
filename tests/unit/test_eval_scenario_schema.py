"""CI tests for the eval scenario schema/loader and the runner's pure helpers.

Only the schema/loader and the runner's pure helpers (``find_malformed``,
``tool_result_emptiness``, ``evaluate_assertions``) are CI-tested — the
runner itself needs real Ollama models and is exercised manually (see
``evals/README.md``). No litellm/Ollama/machina imports anywhere in this
module (``evals.conversational.run`` imports machina only under
``TYPE_CHECKING``); the loader is dependency-light by design
(stdlib + PyYAML, a core dependency).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evals.conversational.run import (
    TurnSignals,
    evaluate_assertions,
    find_malformed,
    preflight_model,
    tool_result_emptiness,
)
from evals.conversational.schema import (
    ASSERTION_LAYERS,
    ASSERTION_ORDER,
    LAYER_ORDER,
    LONG_SCENARIO_MIN_TURNS,
    Scenario,
    ScenarioSchemaError,
    TurnAssertions,
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

    def test_diagnosis_flow_scenario_carries_tool_result_assertion(self) -> None:
        """The tightened diagnosis scenario asserts on the tool's RESULT.

        Context echo (asset context lists the failure-mode codes) can fake
        the golden assertion; only the tool-result assertion distinguishes a
        working diagnose_failure from a stub.
        """
        scenario = load_scenario(SCENARIOS_DIR / "diagnosis-flow.yaml")
        assertions = scenario.turns[0].assertions
        assert assertions.expect_tool_invoked == "diagnose_failure"
        assert assertions.expect_tool_result_nonempty == "diagnose_failure"
        assert "BEAR-WEAR-01" in assertions.golden_contains


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

    def test_expect_not_fallback_parses(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "assertions": {"expect_not_fallback": True}}])
        scenario = parse_scenario(data)
        assertions = scenario.turns[0].assertions
        assert assertions.expect_not_fallback is True
        assert ("expect_not_fallback", True) in assertions.active_assertions()
        # Inactive by default: a turn that never mentions it asserts nothing.
        default = parse_scenario(_valid_data()).turns[0].assertions
        assert default.expect_not_fallback is None
        assert all(n != "expect_not_fallback" for n, _ in default.active_assertions())

    def test_expect_not_fallback_must_be_boolean(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "assertions": {"expect_not_fallback": "yes"}}])
        with pytest.raises(ScenarioSchemaError, match="expect_not_fallback"):
            parse_scenario(data)

    def test_golden_contains_must_be_string_list(self) -> None:
        data = _valid_data(turns=[{"user": "hi", "assertions": {"golden_contains": "P-201"}}])
        with pytest.raises(ScenarioSchemaError, match="golden_contains"):
            parse_scenario(data)

    def test_expect_tool_result_nonempty_parses(self) -> None:
        data = _valid_data(
            turns=[
                {"user": "hi", "assertions": {"expect_tool_result_nonempty": "diagnose_failure"}}
            ]
        )
        assertions = parse_scenario(data).turns[0].assertions
        assert assertions.expect_tool_result_nonempty == "diagnose_failure"
        assert ("expect_tool_result_nonempty", "diagnose_failure") in (
            assertions.active_assertions()
        )
        # Inactive by default: a turn that never mentions it asserts nothing.
        default = parse_scenario(_valid_data()).turns[0].assertions
        assert default.expect_tool_result_nonempty is None
        assert all(n != "expect_tool_result_nonempty" for n, _ in default.active_assertions())

    @pytest.mark.parametrize("bad", [True, 7, ["diagnose_failure"], "", "   "])
    def test_expect_tool_result_nonempty_rejects_non_string_and_empty(self, bad: object) -> None:
        data = _valid_data(
            turns=[{"user": "hi", "assertions": {"expect_tool_result_nonempty": bad}}]
        )
        with pytest.raises(ScenarioSchemaError, match="expect_tool_result_nonempty"):
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
            "expect_tool_result_nonempty": "runtime",
            "expect_no_malformed": "runtime",
            "expect_not_fallback": "runtime",
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
                            "expect_not_fallback": True,
                            "expect_tool_invoked": "list_assets",
                            "expect_tool_result_nonempty": "list_assets",
                            "expect_retrieval_source": "manual",
                        },
                    }
                ]
            )
        )
        names = [n for n, _ in scenario.turns[0].assertions.active_assertions()]
        assert names == [
            "expect_tool_invoked",
            "expect_tool_result_nonempty",
            "expect_no_malformed",
            "expect_not_fallback",
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


class TestPreflightColonlessTags:
    """Colon-less model tags are preflighted, never left for litellm to reject.

    ``preflight_model`` on a colon-less tag is pure env-var inspection (no
    subprocess, no network), so it IS CI-testable even though the runner is
    not. Regression: ``--models "llama3"`` used to fall through every branch
    and produce one litellm "LLM Provider NOT provided" ERROR row per turn
    (observed 2026-06-10); now it skips upfront with a suggestion.
    """

    def test_unknown_colonless_tag_skipped_with_ollama_suggestion(self) -> None:
        error = preflight_model("llama3")
        assert error is not None
        assert "no provider" in error
        assert "ollama:llama3" in error

    def test_gpt_tag_without_key_names_the_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        error = preflight_model("gpt-4o")
        assert error is not None
        assert "OPENAI_API_KEY" in error

    def test_gpt_tag_with_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert preflight_model("gpt-4o") is None

    def test_claude_tag_without_key_names_the_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        error = preflight_model("claude-sonnet-4-5")
        assert error is not None
        assert "ANTHROPIC_API_KEY" in error

    def test_claude_tag_with_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert preflight_model("claude-sonnet-4-5") is None

    def test_explicit_provider_prefix_is_not_affected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tags with an explicit non-ollama provider still preflight as before."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert preflight_model("openai:gpt-4o") is None
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert preflight_model("openai:gpt-4o") is not None


class TestFindMalformed:
    """The runner's malformed-output sniff covers all three leak shapes.

    ``find_malformed`` is a pure helper (regex + ``json.loads``) — it needs
    no Ollama model, so it IS CI-testable even though the runner is not.
    Shape C (string-valued ``"function"`` key, deepseek-r1:8b baseline
    2026-06-10) is covered both inline (``_TOOL_JSON_RE``) and as a bare
    whole-response object (the ``has_name`` branch).
    """

    def test_inline_shape_c_json_in_prose_is_flagged(self) -> None:
        text = (
            "Let me check that for you: "
            '{"function": "get_asset_details", "arguments": {"asset_id": "P-201"}} '
            "and I will report back."
        )
        assert find_malformed(text) == "tool-call-shaped JSON in output"

    def test_bare_shape_c_object_function_key_first_is_flagged(self) -> None:
        # With the name key BEFORE the arguments key the inline regex already
        # matches, so the whole-response payload is flagged by that check.
        text = '{"function": "get_asset_details", "arguments": {"asset_id": "P-201"}}'
        assert find_malformed(text) == "tool-call-shaped JSON in output"

    def test_bare_shape_c_object_arguments_key_first_is_flagged(self) -> None:
        # Arguments-first key order defeats the inline regex (which requires
        # name-before-arguments), so this exercises the bare-object
        # ``json.loads`` branch and its shape-C ``has_name`` arm.
        text = '{"arguments": {"asset_id": "P-201"}, "function": "get_asset_details"}'
        assert find_malformed(text) == "response is a bare tool-call JSON object"

    def test_plain_data_json_with_function_string_but_no_args_is_clean(self) -> None:
        # A string "function" key WITHOUT an arguments-like key is data, not a
        # call — must not be flagged (mirrors the runtime detector's R9 rule).
        text = '{"function": "filtering", "description": "how the filter stage works"}'
        assert find_malformed(text) is None

    def test_ordinary_prose_is_clean(self) -> None:
        assert find_malformed("P-201 needs a bearing replacement within 48 hours.") is None

    def test_shape_a_payload_still_detected(self) -> None:
        text = (
            '{"type": "function", "function": '
            '{"name": "search_assets", "arguments": {"query": "pump"}}}'
        )
        assert find_malformed(text) == "tool-call-shaped JSON in output"

    def test_shape_b_payload_still_detected(self) -> None:
        text = '{"name": "search_assets", "arguments": {"query": "pump"}}'
        assert find_malformed(text) == "tool-call-shaped JSON in output"


class TestToolResultEmptiness:
    """The meaningful-emptiness heuristic behind ``expect_tool_result_nonempty``.

    Pure helper (``json.loads`` + a key probe) — CI-testable without models.
    """

    def test_diagnose_failure_with_matches_is_nonempty(self) -> None:
        raw = (
            '{"asset_id": "P-201", "symptoms": ["high vibration"], '
            '"probable_failures": [{"code": "BEAR-WEAR-01", "confidence": 0.67}]}'
        )
        assert tool_result_emptiness(raw) is None

    def test_diagnose_failure_stub_shape_is_empty_despite_truthy_dict(self) -> None:
        # The envelope (asset_id, symptoms echo, note) makes the dict truthy,
        # but the single obvious list payload is empty — exactly the old-stub
        # shape the assertion exists to catch.
        raw = (
            '{"asset_id": "P-201", "symptoms": ["high vibration"], '
            '"probable_failures": [], "note": "No catalog entry matched."}'
        )
        assert tool_result_emptiness(raw) == "'probable_failures' list is empty"

    @pytest.mark.parametrize("raw", ["{}", "[]", "null", '""', "", "   "])
    def test_falsy_payloads_are_empty(self, raw: str) -> None:
        assert tool_result_emptiness(raw) is not None

    def test_generic_results_key_checked(self) -> None:
        assert tool_result_emptiness('{"query": "pump", "results": []}') is not None
        assert tool_result_emptiness('{"query": "pump", "results": [{"id": 1}]}') is None

    def test_dict_without_list_payload_uses_truthiness(self) -> None:
        assert tool_result_emptiness('{"status": "ok"}') is None

    def test_two_list_payload_keys_one_nonempty_is_nonempty(self) -> None:
        # Mixed shape (two candidate list keys, one carries data): the result
        # is meaningfully non-empty.
        raw = '{"results": [], "items": [{"id": 1}]}'
        assert tool_result_emptiness(raw) is None

    def test_multiple_list_payload_keys_all_empty_is_empty(self) -> None:
        # More than one known list-payload key, ALL empty — must report empty,
        # not silently fall back to dict truthiness.
        raw = '{"results": [], "items": [], "note": "nothing"}'
        diagnosis = tool_result_emptiness(raw)
        assert diagnosis is not None
        assert "results" in diagnosis and "items" in diagnosis

    def test_error_key_reports_failed_call(self) -> None:
        # A dict carrying 'error' is a FAILED call, however truthy the dict.
        raw = '{"error": "No CMMS connector available"}'
        diagnosis = tool_result_emptiness(raw)
        assert diagnosis is not None
        assert "failed" in diagnosis
        assert "No CMMS connector available" in diagnosis

    def test_replay_envelope_recurses_into_result(self) -> None:
        # The runtime's duplicate-read replay envelope wraps the original
        # payload under 'result' — emptiness is judged on the inner payload.
        nonempty = (
            '{"already_retrieved": true, "note": "duplicate read", '
            '"result": {"probable_failures": [{"code": "BEAR-WEAR-01"}]}}'
        )
        assert tool_result_emptiness(nonempty) is None
        empty = (
            '{"already_retrieved": true, "note": "duplicate read", '
            '"result": {"asset_id": "P-201", "probable_failures": []}}'
        )
        assert tool_result_emptiness(empty) == "'probable_failures' list is empty"

    def test_already_executed_envelope_recurses_into_result(self) -> None:
        # The duplicate-write envelope uses 'already_executed' instead.
        raw = '{"already_executed": true, "note": "duplicate write", "result": {}}'
        assert tool_result_emptiness(raw) is not None

    def test_string_valued_probable_failures_counts_as_nonempty(self) -> None:
        # Documentation case: a STRING under a known payload key does not
        # participate in the list probe — dict truthiness decides (non-empty).
        raw = '{"probable_failures": "see attached analysis", "asset_id": "P-201"}'
        assert tool_result_emptiness(raw) is None

    def test_unparseable_recording_falls_back_to_string_truthiness(self) -> None:
        # Legacy truncated output_summary (repr-style, not JSON).
        assert tool_result_emptiness("{'asset_id': 'P-201', 'probable_fail") is None

    def test_nonempty_list_payload_root_is_nonempty(self) -> None:
        assert tool_result_emptiness('[{"id": "P-201"}]') is None


class TestExpectToolResultNonemptyEvaluation:
    """Runtime-layer evaluation of ``expect_tool_result_nonempty``."""

    @staticmethod
    def _eval(signals: TurnSignals) -> tuple[bool, str, str]:
        assertions = TurnAssertions(
            expect_tool_result_nonempty="diagnose_failure", expect_no_malformed=False
        )
        results = evaluate_assertions(assertions, signals)
        assert len(results) == 1
        r = results[0]
        assert r.name == "expect_tool_result_nonempty"
        assert r.layer == "runtime"
        return r.passed, r.expected, r.actual

    def test_passes_on_nonempty_recorded_result(self) -> None:
        signals = TurnSignals(
            text="Probable failure mode: BEAR-WEAR-01.",
            invoked_tools=("diagnose_failure",),
            tool_results=(
                ("diagnose_failure", '{"probable_failures": [{"code": "BEAR-WEAR-01"}]}'),
            ),
        )
        passed, _, _ = self._eval(signals)
        assert passed is True

    def test_fails_when_tool_not_invoked_even_if_text_echoes_the_code(self) -> None:
        # Context echo: the answer names BEAR-WEAR-01 (it is in the injected
        # asset context) but the tool never ran — must FAIL.
        signals = TurnSignals(
            text="Probably BEAR-WEAR-01, based on the known failure modes.",
            invoked_tools=("search_assets",),
            tool_results=(("search_assets", '[{"id": "P-201"}]'),),
        )
        passed, _, actual = self._eval(signals)
        assert passed is False
        assert "diagnose_failure" in actual
        assert "not invoked" in actual

    def test_fails_on_empty_payload_and_evidence_names_tool_and_emptiness(self) -> None:
        signals = TurnSignals(
            text="Probably BEAR-WEAR-01.",
            invoked_tools=("diagnose_failure",),
            tool_results=(("diagnose_failure", '{"asset_id": "P-201", "probable_failures": []}'),),
        )
        passed, expected, actual = self._eval(signals)
        assert passed is False
        assert "diagnose_failure" in expected
        assert "diagnose_failure" in actual
        assert "'probable_failures' list is empty" in actual

    def test_any_nonempty_invocation_passes_when_tool_ran_twice(self) -> None:
        signals = TurnSignals(
            text="BEAR-WEAR-01.",
            invoked_tools=("diagnose_failure", "diagnose_failure"),
            tool_results=(
                ("diagnose_failure", '{"probable_failures": []}'),
                ("diagnose_failure", '{"probable_failures": [{"code": "BEAR-WEAR-01"}]}'),
            ),
        )
        passed, _, _ = self._eval(signals)
        assert passed is True


class TestFindMalformedDegenerateJson:
    """The sniff observes the degenerate empty-JSON answer mode.

    2026-06-10 post-fix deepseek-r1:8b eval: 7 turns whose final answer was
    literally ``{}`` (typically after a leaked-read recovery) were attributed
    'unattributed'/golden because no observable signal claimed them. The
    sniff mirrors the runtime's ``_finalize_turn`` degenerate-JSON guard so
    reports attribute the mode to the runtime layer.
    """

    @pytest.mark.parametrize("text", ["{}", "[]", "  {}  ", "{ }", "[\n]"])
    def test_empty_json_container_is_flagged(self, text: str) -> None:
        assert find_malformed(text) == "degenerate empty-JSON answer"

    def test_nonempty_data_json_is_clean(self) -> None:
        assert find_malformed('{"status": "ok", "open_work_orders": 3}') is None

    def test_json_null_is_clean(self) -> None:
        # Non-container JSON is out of scope, mirroring the runtime guard.
        assert find_malformed("null") is None

    def test_prose_mentioning_braces_is_clean(self) -> None:
        assert find_malformed("Use {} as the default config value.") is None
