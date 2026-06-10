"""U8 — Malformed-output corpus: data-driven regression cases (R11/R12).

Every observed malformed-output failure mode lives as one JSON fixture in
``corpus/``. This module's single parametrized test discovers ``corpus/*.json``,
replays each fixture's scripted completions through a fake provider against a
REAL :class:`Agent` (full ``handle_message_full`` turns — loop seam plus the
``_finalize_turn`` gate, end to end), and asserts the expected disposition
against the returned ``AgentResponse`` fields (``text`` / ``is_fallback`` /
``completeness``) — never via log capture.

Adding a future case requires only a new fixture file, no code edit (R12).
The fixture schema, the turn-to-provider-call mapping, and the process rule
("a newly discovered malformed-output failure mode lands as a fixture before
or with its fix") are documented in ``corpus/README.md``. A fixture with an
unknown key or disposition fails LOUDLY via the schema guard below, so corpus
rot is caught instead of silently skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, NoReturn

import pytest

from machina.agent.runtime import (
    _EMPTY_RESPONSE_FALLBACK,
    _REPEATED_RESPONSE_FALLBACK,
    _TOOL_CALL_LEAK_FALLBACK,
    Agent,
)
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant

CORPUS_DIR = Path(__file__).parent / "corpus"
FIXTURE_PATHS = sorted(CORPUS_DIR.glob("*.json"))

# Used when a fixture omits ``user_messages`` — one generic probe turn.
_DEFAULT_USER_MESSAGE = "Tell me about P-201"

# The initial corpus (every malformed-output mode observed or pinned to date).
# Guards against discovery rot (e.g. a moved directory making the glob match
# nothing and the parametrized test silently collect zero cases). New fixtures
# do NOT need to be listed here — this is a floor, not a registry.
_INITIAL_CASE_IDS = frozenset(
    {
        "leaked-known-read-recovered",
        "leaked-known-write-suppressed",
        "hallucinated-tool-suppressed",
        "prior-turn-echo",
        "unclosed-citations-tag",
        "empty-completion",
        "tool-result-echo",
        "force-final-leak",
        "deepseek-think-block",
        "legit-json-answer-pin",
    }
)

# ---------------------------------------------------------------------------
# Fixture schema guard — unknown keys fail LOUDLY so corpus rot is caught.
# ---------------------------------------------------------------------------

_TOP_LEVEL_REQUIRED = frozenset({"id", "description", "turns", "expected"})
_TOP_LEVEL_ALLOWED = _TOP_LEVEL_REQUIRED | {"user_messages"}
_EXPECTED_REQUIRED = frozenset({"disposition"})
_EXPECTED_ALLOWED = _EXPECTED_REQUIRED | {
    "user_text_contains",
    "user_text_excludes",
    "is_fallback",
    "completeness",
}
_TURN_ALLOWED = frozenset({"content", "tool_calls"})
_TOOL_CALL_KEYS = frozenset({"name", "arguments"})
_DISPOSITIONS = frozenset(
    {
        "clean",
        "recovered_read",
        "fallback_leak",
        "fallback_leak_write",
        "fallback_empty",
        "fallback_echo",
    }
)
_COMPLETENESS_VALUES = frozenset({"complete", "partial"})


class CorpusFixtureError(Exception):
    """A corpus fixture violates the documented schema (see corpus/README.md)."""


def _fail(name: str, message: str) -> NoReturn:
    raise CorpusFixtureError(f"{name}: {message}")


def _validate_fixture(data: Any, name: str) -> dict[str, Any]:
    """Validate a fixture against the documented schema; fail loudly otherwise."""
    if not isinstance(data, dict):
        _fail(name, "fixture root must be a JSON object")

    unknown = set(data) - _TOP_LEVEL_ALLOWED
    if unknown:
        _fail(
            name,
            f"unknown top-level key(s) {sorted(unknown)}; allowed: {sorted(_TOP_LEVEL_ALLOWED)}",
        )
    missing = _TOP_LEVEL_REQUIRED - set(data)
    if missing:
        _fail(name, f"missing required top-level key(s) {sorted(missing)}")

    if not isinstance(data["id"], str) or not data["id"]:
        _fail(name, "'id' must be a non-empty string")
    if not isinstance(data["description"], str) or not data["description"]:
        _fail(name, "'description' must be a non-empty string")

    user_messages = data.get("user_messages")
    if user_messages is not None and (
        not isinstance(user_messages, list)
        or not user_messages
        or not all(isinstance(m, str) and m for m in user_messages)
    ):
        _fail(name, "'user_messages' must be a non-empty list of non-empty strings")

    turns = data["turns"]
    if not isinstance(turns, list) or not turns:
        _fail(name, "'turns' must be a non-empty list")
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            _fail(name, f"turns[{i}] must be an object")
        unknown_turn = set(turn) - _TURN_ALLOWED
        if unknown_turn:
            _fail(
                name,
                f"turns[{i}] has unknown key(s) {sorted(unknown_turn)}; "
                f"allowed: {sorted(_TURN_ALLOWED)}",
            )
        content = turn.get("content")
        if content is not None and not isinstance(content, str):
            _fail(name, f"turns[{i}].content must be a string or null")
        tool_calls = turn.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list) or not tool_calls:
                _fail(name, f"turns[{i}].tool_calls must be a non-empty list or null")
            for j, call in enumerate(tool_calls):
                if not isinstance(call, dict) or set(call) != _TOOL_CALL_KEYS:
                    _fail(
                        name,
                        f"turns[{i}].tool_calls[{j}] must be an object with exactly "
                        f"the keys {sorted(_TOOL_CALL_KEYS)}",
                    )
                if not isinstance(call["name"], str) or not call["name"]:
                    _fail(name, f"turns[{i}].tool_calls[{j}].name must be a non-empty string")
                if not isinstance(call["arguments"], str):
                    _fail(
                        name,
                        f"turns[{i}].tool_calls[{j}].arguments must be a JSON-encoded "
                        "STRING (the provider wire format), not an object",
                    )

    expected = data["expected"]
    if not isinstance(expected, dict):
        _fail(name, "'expected' must be an object")
    unknown_expected = set(expected) - _EXPECTED_ALLOWED
    if unknown_expected:
        _fail(
            name,
            f"unknown expected key(s) {sorted(unknown_expected)}; "
            f"allowed: {sorted(_EXPECTED_ALLOWED)}",
        )
    missing_expected = _EXPECTED_REQUIRED - set(expected)
    if missing_expected:
        _fail(name, f"missing required expected key(s) {sorted(missing_expected)}")
    if expected["disposition"] not in _DISPOSITIONS:
        _fail(
            name,
            f"unknown disposition {expected['disposition']!r}; allowed: {sorted(_DISPOSITIONS)}",
        )
    for key in ("user_text_contains", "user_text_excludes"):
        value = expected.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(s, str) and s for s in value)
        ):
            _fail(name, f"expected.{key} must be a list of non-empty strings")
    if "is_fallback" in expected and not isinstance(expected["is_fallback"], bool):
        _fail(name, "expected.is_fallback must be a boolean")
    if "completeness" in expected and expected["completeness"] not in _COMPLETENESS_VALUES:
        _fail(name, f"expected.completeness must be one of {sorted(_COMPLETENESS_VALUES)}")

    return data


def _load_fixture(path: Path) -> dict[str, Any]:
    """Read, schema-validate, and id-check a fixture file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(path.name, f"not valid JSON: {exc}")
    case = _validate_fixture(data, path.name)
    if case["id"] != path.stem:
        _fail(path.name, f"fixture id {case['id']!r} must equal the filename stem {path.stem!r}")
    return case


# ---------------------------------------------------------------------------
# Replay harness — scripted provider, spy connectors, plant wiring.
# ---------------------------------------------------------------------------


def _plant() -> Plant:
    plant = Plant(name="Test Plant")
    plant.register_asset(
        Asset(
            id="P-201",
            name="Cooling Water Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building A",
            criticality=Criticality.A,
        )
    )
    return plant


class _ReadAssetsConnector:
    """READ_ASSETS connector so asset read tools are offered to the loop."""

    capabilities: ClassVar[list[str]] = ["read_assets"]

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def read_assets(self, **kwargs: Any) -> list[Asset]:
        return list(_plant().assets.values())


class _SpyWriteConnector:
    """CREATE_WORK_ORDER connector recording whether any write ever executed.

    Wired into EVERY corpus case: no fixture may legitimately mutate anything,
    so ``created == 0`` is asserted universally (and explicitly for the
    ``fallback_leak_write`` disposition).
    """

    capabilities: ClassVar[list[str]] = ["create_work_order"]

    def __init__(self) -> None:
        self.created = 0

    async def connect(self) -> None:  # pragma: no cover
        pass

    async def disconnect(self) -> None:  # pragma: no cover
        pass

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    async def create_work_order(self, wo: Any) -> Any:  # pragma: no cover
        self.created += 1
        return wo


class _ScriptedLLM:
    """Replays a fixture's ``turns`` as scripted completions, strictly in order.

    Every provider call consumes the NEXT turn, whichever method the runtime
    invokes: ``complete_with_tools`` is what ``_llm_loop`` calls while tools
    are offered; ``complete`` is the forced-final no-tools completion after a
    break or iteration exhaustion. A fixture exercising the force-finalization
    path therefore simply places the forced completion last. Fixture
    ``tool_calls`` become duck-typed objects with ``.function.name`` /
    ``.function.arguments`` / ``.id`` (the shape the loop consumes). Asking
    for more completions than scripted — or finishing with turns left over —
    fails loudly, so a mis-scripted fixture cannot pass by accident.
    """

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.model = "fake:scripted-corpus"
        self._turns = turns
        self._consumed = 0

    def _next_turn(self, method: str) -> dict[str, Any]:
        if self._consumed >= len(self._turns):
            raise AssertionError(
                f"fixture script exhausted: the runtime requested completion "
                f"#{self._consumed + 1} via {method}() but only "
                f"{len(self._turns)} turn(s) are scripted"
            )
        turn = self._turns[self._consumed]
        self._consumed += 1
        return turn

    def assert_exhausted(self) -> None:
        assert self._consumed == len(self._turns), (
            f"fixture scripted {len(self._turns)} turn(s) but the runtime "
            f"consumed only {self._consumed} — the script does not match the "
            "replayed behaviour"
        )

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        turn = self._next_turn("complete")
        if turn.get("tool_calls"):
            raise AssertionError(
                "a turn consumed by complete() (the forced-final no-tools call) "
                "must be text-only — remove its tool_calls"
            )
        return turn.get("content") or ""

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        turn = self._next_turn("complete_with_tools")
        raw_calls = turn.get("tool_calls")
        tool_calls = None
        if raw_calls:
            tool_calls = [
                SimpleNamespace(
                    function=SimpleNamespace(name=call["name"], arguments=call["arguments"]),
                    id=f"call_{self._consumed:03d}_{j}",
                )
                for j, call in enumerate(raw_calls, 1)
            ]
        return {"content": turn.get("content") or "", "tool_calls": tool_calls}


def _assert_disposition(response: Any, expected: dict[str, Any], spy: _SpyWriteConnector) -> None:
    """Assert the expected disposition against the final ``AgentResponse``.

    Behaviour only — fields of the response object, never log capture.
    """
    disposition = expected["disposition"]

    # Universal invariant: no corpus fixture may ever execute a write.
    assert spy.created == 0, (
        f"a corpus replay executed {spy.created} write(s) — no fixture may "
        "legitimately mutate anything"
    )

    if disposition in ("fallback_leak", "fallback_leak_write"):
        # The user-facing text AND the structured flag agree on BOTH
        # suppression paths: a loop-seam suppression substitutes the fallback
        # text and _finalize_turn recognises the sentinel and sets
        # ``is_fallback``; the gate's own backstop sets both directly.
        assert response.text == _TOOL_CALL_LEAK_FALLBACK
        assert response.is_fallback is True
    elif disposition == "fallback_empty":
        assert response.text == _EMPTY_RESPONSE_FALLBACK
        assert response.is_fallback is True
    elif disposition == "fallback_echo":
        assert response.text == _REPEATED_RESPONSE_FALLBACK
        assert response.is_fallback is True
    else:  # "clean" / "recovered_read" — a real answer was delivered.
        assert response.is_fallback is False

    if "is_fallback" in expected:
        assert response.is_fallback is expected["is_fallback"]
    if "completeness" in expected:
        assert response.completeness == expected["completeness"]
    for needle in expected.get("user_text_contains", []):
        assert needle in response.text, f"expected {needle!r} in user text"
    for needle in expected.get("user_text_excludes", []):
        assert needle not in response.text, f"expected {needle!r} NOT in user text"


# ---------------------------------------------------------------------------
# The corpus test — one parametrized replay per fixture file.
# ---------------------------------------------------------------------------


def test_corpus_discovery_found_initial_cases() -> None:
    """Discovery rot guard: the glob found the corpus and its known floor.

    New fixtures need NO edit here — this asserts a floor (the initial,
    documented failure modes), not a closed registry.
    """
    found = {p.stem for p in FIXTURE_PATHS}
    missing = _INITIAL_CASE_IDS - found
    assert not missing, f"corpus fixtures missing from {CORPUS_DIR}: {sorted(missing)}"


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=[p.stem for p in FIXTURE_PATHS])
@pytest.mark.asyncio
async def test_malformed_output_case(fixture_path: Path) -> None:
    """Replay one corpus fixture end-to-end and assert its disposition."""
    case = _load_fixture(fixture_path)

    llm = _ScriptedLLM(case["turns"])
    spy = _SpyWriteConnector()
    agent = Agent(plant=_plant(), llm=llm, connectors=[_ReadAssetsConnector(), spy])

    response: Any = None
    for message in case.get("user_messages") or [_DEFAULT_USER_MESSAGE]:
        response = await agent.handle_message_full(message, chat_id="corpus")

    assert response is not None
    llm.assert_exhausted()
    _assert_disposition(response, case["expected"], spy)


# ---------------------------------------------------------------------------
# Schema guard self-tests — the guard itself must fail loudly, not rot.
# ---------------------------------------------------------------------------


def _minimal_fixture(**overrides: Any) -> dict[str, Any]:
    """A schema-valid fixture dict, overridable per test."""
    fixture: dict[str, Any] = {
        "id": "guard-case",
        "description": "schema-guard self-test fixture",
        "turns": [{"content": "A perfectly ordinary answer.", "tool_calls": None}],
        "expected": {"disposition": "clean"},
    }
    fixture.update(overrides)
    return fixture


class TestSchemaGuard:
    """The loader rejects malformed fixtures with a loud, named error."""

    @staticmethod
    def _write(tmp_path: Path, data: dict[str, Any], stem: str = "guard-case") -> Path:
        path = tmp_path / f"{stem}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def test_valid_minimal_fixture_loads(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, _minimal_fixture())
        case = _load_fixture(path)
        assert case["id"] == "guard-case"

    def test_unknown_top_level_key_fails_loudly(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, _minimal_fixture(bogus_key="surprise"))
        with pytest.raises(CorpusFixtureError, match="bogus_key"):
            _load_fixture(path)

    def test_unknown_expected_key_fails_loudly(self, tmp_path: Path) -> None:
        bad = _minimal_fixture(expected={"disposition": "clean", "asserts_logs": True})
        path = self._write(tmp_path, bad)
        with pytest.raises(CorpusFixtureError, match="asserts_logs"):
            _load_fixture(path)

    def test_unknown_disposition_fails_loudly(self, tmp_path: Path) -> None:
        bad = _minimal_fixture(expected={"disposition": "fallback_mystery"})
        path = self._write(tmp_path, bad)
        with pytest.raises(CorpusFixtureError, match="fallback_mystery"):
            _load_fixture(path)

    def test_missing_required_key_fails_loudly(self, tmp_path: Path) -> None:
        bad = _minimal_fixture()
        del bad["expected"]
        path = self._write(tmp_path, bad)
        with pytest.raises(CorpusFixtureError, match="expected"):
            _load_fixture(path)

    def test_unknown_turn_key_fails_loudly(self, tmp_path: Path) -> None:
        bad = _minimal_fixture(turns=[{"content": "hi", "tool_calls": None, "role": "assistant"}])
        path = self._write(tmp_path, bad)
        with pytest.raises(CorpusFixtureError, match="role"):
            _load_fixture(path)

    def test_object_arguments_fail_loudly(self, tmp_path: Path) -> None:
        bad = _minimal_fixture(
            turns=[{"content": "", "tool_calls": [{"name": "search_assets", "arguments": {}}]}]
        )
        path = self._write(tmp_path, bad)
        with pytest.raises(CorpusFixtureError, match="JSON-encoded STRING"):
            _load_fixture(path)

    def test_id_filename_mismatch_fails_loudly(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, _minimal_fixture(), stem="another-name")
        with pytest.raises(CorpusFixtureError, match="filename stem"):
            _load_fixture(path)
