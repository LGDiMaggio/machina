"""Scenario schema and loader for the conversational eval harness.

A scenario is a YAML file describing a frozen multi-turn conversation
plus per-turn assertions. Every assertion type maps to exactly ONE
layer (the *layer-attribution contract*, origin requirements R13-R16):

==========================  ==========
Assertion                   Layer
==========================  ==========
``expect_tool_invoked``     runtime
``expect_no_malformed``     runtime
``expect_not_fallback``     runtime
``expect_retrieval_source``  retrieval
``expect_citation``         citations
``golden_contains``         golden
``golden_excludes``         golden
==========================  ==========

Attribution = the layer of the FIRST failed assertion, evaluated in
the fixed order runtime -> retrieval -> citations -> golden. The
runner refines a golden-only failure in a long scenario (10+ turns)
via a truncated-history replay into ``conversation-length`` or
``unattributed`` — never by guessing.

This module is deliberately dependency-light (stdlib + PyYAML, a core
machina dependency): the CI schema test imports it without litellm,
Ollama, or machina present.
"""

from __future__ import annotations

import glob as _glob
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --- Layer-attribution contract (keep run.py and README in sync) ---------

LAYER_ORDER: tuple[str, ...] = ("runtime", "retrieval", "citations", "golden")
"""Evaluation order; the first failed assertion's layer is the attribution."""

ASSERTION_LAYERS: dict[str, str] = {
    "expect_tool_invoked": "runtime",
    "expect_no_malformed": "runtime",
    "expect_not_fallback": "runtime",
    "expect_retrieval_source": "retrieval",
    "expect_citation": "citations",
    "golden_contains": "golden",
    "golden_excludes": "golden",
}
"""Total mapping: every assertion type maps to exactly one layer."""

ASSERTION_ORDER: tuple[str, ...] = (
    "expect_tool_invoked",
    "expect_no_malformed",
    "expect_not_fallback",
    "expect_retrieval_source",
    "expect_citation",
    "golden_contains",
    "golden_excludes",
)
"""Canonical per-turn evaluation order (layer order, then fixed within-layer)."""

LONG_SCENARIO_MIN_TURNS = 10
"""A scenario with at least this many turns qualifies for the length check."""

_TOP_LEVEL_KEYS = frozenset({"id", "description", "connectors", "turns"})
_TURN_KEYS = frozenset({"user", "assertions"})


class ScenarioSchemaError(ValueError):
    """A scenario file violates the schema; the message names the offending field."""


@dataclass(frozen=True)
class TurnAssertions:
    """Per-turn assertions, each tied to exactly one layer.

    Attributes:
        expect_tool_invoked: Tool name that must appear as a traced
            ``tool_call`` during the turn (runtime layer).
        expect_no_malformed: When ``True`` (the default), the response
            text must not contain tool-call-shaped JSON or raw
            ``<think>``/``<citations>`` tags (runtime layer).
        expect_not_fallback: When ``True``, the response must be a real
            answer, not a runtime fallback (``AgentResponse.is_fallback``
            must be ``False``); ``False`` requires a fallback (runtime
            layer).
        expect_retrieval_source: Substring that must appear in at least
            one citation source for the turn (retrieval layer).
        expect_citation: When set, requires (``True``) or forbids
            (``False``) citations on the turn (citations layer).
        golden_contains: Case-insensitive substrings that must all
            appear in the answer text (golden layer).
        golden_excludes: Case-insensitive substrings that must NOT
            appear in the answer text (golden layer).
    """

    expect_tool_invoked: str | None = None
    expect_no_malformed: bool = True
    expect_not_fallback: bool | None = None
    expect_retrieval_source: str | None = None
    expect_citation: bool | None = None
    golden_contains: tuple[str, ...] = ()
    golden_excludes: tuple[str, ...] = ()

    def active_assertions(self) -> list[tuple[str, Any]]:
        """Return ``(assertion_name, value)`` pairs in canonical evaluation order.

        Only assertions that are actually active for this turn are
        returned; ``expect_no_malformed`` is active unless explicitly
        disabled in the scenario file.
        """
        active: list[tuple[str, Any]] = []
        for name in ASSERTION_ORDER:
            value = getattr(self, name)
            if name == "expect_no_malformed":
                if value:
                    active.append((name, True))
            elif name in ("golden_contains", "golden_excludes"):
                if value:
                    active.append((name, value))
            elif value is not None:
                active.append((name, value))
        return active


@dataclass(frozen=True)
class Turn:
    """A single scripted user turn with its assertions.

    Attributes:
        user: The user message. Self-contained by contract (R13): it
            never references model-specific content from a prior answer.
        assertions: The turn's layered assertions.
    """

    user: str
    assertions: TurnAssertions = field(default_factory=TurnAssertions)


@dataclass(frozen=True)
class Scenario:
    """A frozen multi-turn conversation scenario.

    Attributes:
        id: Unique scenario identifier.
        description: Human-readable purpose of the scenario.
        turns: The scripted turns, in order.
        connectors: ``False`` for the paired no-connector control
            (the agent is built without CMMS/DocumentStore connectors).
        source_path: File the scenario was loaded from, when known.
    """

    id: str
    description: str
    turns: tuple[Turn, ...]
    connectors: bool = True
    source_path: Path | None = None

    @property
    def is_long(self) -> bool:
        """Whether the scenario qualifies for conversation-length attribution."""
        return len(self.turns) >= LONG_SCENARIO_MIN_TURNS


def _fail(source: str, message: str) -> ScenarioSchemaError:
    return ScenarioSchemaError(f"{source}: {message}")


def _require_str(value: Any, *, source: str, fieldname: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(source, f"field '{fieldname}' must be a non-empty string, got {value!r}")
    return value


def _require_bool(value: Any, *, source: str, fieldname: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(source, f"field '{fieldname}' must be a boolean, got {value!r}")
    return value


def _require_str_list(value: Any, *, source: str, fieldname: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _fail(
            source, f"field '{fieldname}' must be a non-empty list of strings, got {value!r}"
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _fail(
                source, f"field '{fieldname}' must contain only non-empty strings, got {item!r}"
            )
    return tuple(value)


def _parse_assertions(raw: Any, *, source: str) -> TurnAssertions:
    if raw is None:
        return TurnAssertions()
    if not isinstance(raw, dict):
        raise _fail(source, f"field 'assertions' must be a mapping, got {raw!r}")

    unknown = sorted(set(raw) - set(ASSERTION_LAYERS))
    if unknown:
        allowed = ", ".join(ASSERTION_ORDER)
        raise _fail(
            source,
            f"unknown assertion key '{unknown[0]}' (allowed: {allowed})",
        )

    kwargs: dict[str, Any] = {}
    if "expect_tool_invoked" in raw:
        kwargs["expect_tool_invoked"] = _require_str(
            raw["expect_tool_invoked"], source=source, fieldname="expect_tool_invoked"
        )
    if "expect_no_malformed" in raw:
        kwargs["expect_no_malformed"] = _require_bool(
            raw["expect_no_malformed"], source=source, fieldname="expect_no_malformed"
        )
    if "expect_not_fallback" in raw:
        kwargs["expect_not_fallback"] = _require_bool(
            raw["expect_not_fallback"], source=source, fieldname="expect_not_fallback"
        )
    if "expect_retrieval_source" in raw:
        kwargs["expect_retrieval_source"] = _require_str(
            raw["expect_retrieval_source"], source=source, fieldname="expect_retrieval_source"
        )
    if "expect_citation" in raw:
        kwargs["expect_citation"] = _require_bool(
            raw["expect_citation"], source=source, fieldname="expect_citation"
        )
    if "golden_contains" in raw:
        kwargs["golden_contains"] = _require_str_list(
            raw["golden_contains"], source=source, fieldname="golden_contains"
        )
    if "golden_excludes" in raw:
        kwargs["golden_excludes"] = _require_str_list(
            raw["golden_excludes"], source=source, fieldname="golden_excludes"
        )
    return TurnAssertions(**kwargs)


def _parse_turn(raw: Any, *, index: int, source: str) -> Turn:
    turn_source = f"{source} (turn {index})"
    if not isinstance(raw, dict):
        raise _fail(turn_source, f"each turn must be a mapping, got {raw!r}")

    unknown = sorted(set(raw) - _TURN_KEYS)
    if unknown:
        raise _fail(
            turn_source,
            f"unknown turn key '{unknown[0]}' (allowed: user, assertions)",
        )
    if "user" not in raw:
        raise _fail(turn_source, "missing required field 'user'")

    user = _require_str(raw["user"], source=turn_source, fieldname="user")
    assertions = _parse_assertions(raw.get("assertions"), source=turn_source)
    return Turn(user=user, assertions=assertions)


def parse_scenario(data: Any, *, source: str = "<scenario>") -> Scenario:
    """Validate raw YAML data and build a :class:`Scenario`.

    Args:
        data: The deserialized YAML document (must be a mapping).
        source: Label used in error messages (file path or ``<scenario>``).

    Returns:
        The validated scenario.

    Raises:
        ScenarioSchemaError: On any schema violation; the message names
            the offending field.
    """
    if not isinstance(data, dict):
        raise _fail(source, f"scenario root must be a mapping, got {type(data).__name__}")

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise _fail(
            source,
            f"unknown top-level key '{unknown[0]}' (allowed: id, description, connectors, turns)",
        )
    for required in ("id", "description", "turns"):
        if required not in data:
            raise _fail(source, f"missing required field '{required}'")

    scenario_id = _require_str(data["id"], source=source, fieldname="id")
    description = _require_str(data["description"], source=source, fieldname="description")
    connectors = True
    if "connectors" in data:
        connectors = _require_bool(data["connectors"], source=source, fieldname="connectors")

    raw_turns = data["turns"]
    if not isinstance(raw_turns, list) or not raw_turns:
        raise _fail(source, f"field 'turns' must be a non-empty list, got {raw_turns!r}")
    turns = tuple(
        _parse_turn(raw, index=i, source=source) for i, raw in enumerate(raw_turns, start=1)
    )
    return Scenario(id=scenario_id, description=description, turns=turns, connectors=connectors)


def load_scenario(path: Path | str) -> Scenario:
    """Load and validate a single scenario YAML file.

    Args:
        path: Path to the scenario file.

    Returns:
        The validated scenario, with :attr:`Scenario.source_path` set.

    Raises:
        ScenarioSchemaError: If the file does not parse as YAML or
            violates the schema.
    """
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _fail(str(p), f"not valid YAML: {exc}") from exc
    except OSError as exc:
        raise _fail(str(p), f"cannot read file: {exc}") from exc
    scenario = parse_scenario(raw, source=str(p))
    return Scenario(
        id=scenario.id,
        description=scenario.description,
        turns=scenario.turns,
        connectors=scenario.connectors,
        source_path=p,
    )


def load_scenarios(spec: Path | str) -> list[Scenario]:
    """Load every scenario matching a directory, file, or glob pattern.

    Args:
        spec: A directory (loads all ``*.yaml``/``*.yml`` inside), a
            single file, or a glob pattern.

    Returns:
        Validated scenarios, sorted by file name.

    Raises:
        ScenarioSchemaError: If nothing matches, any file fails
            validation, or two scenarios share an ``id``.
    """
    p = Path(spec)
    if p.is_dir():
        files = sorted([*p.glob("*.yaml"), *p.glob("*.yml")])
    elif p.is_file():
        files = [p]
    else:
        files = [Path(f) for f in sorted(_glob.glob(str(spec), recursive=True))]
    if not files:
        raise ScenarioSchemaError(f"no scenario files found for {str(spec)!r}")

    scenarios = [load_scenario(f) for f in files]
    seen: dict[str, Path | None] = {}
    for s in scenarios:
        if s.id in seen:
            raise ScenarioSchemaError(
                f"duplicate scenario id '{s.id}' in {s.source_path} (first seen in {seen[s.id]})"
            )
        seen[s.id] = s.source_path
    return scenarios
