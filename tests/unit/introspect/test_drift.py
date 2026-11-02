"""Drift gate for the committed Form-A capability artifacts.

These tests make staleness structurally impossible: they regenerate the
artifact from the **live** core (the same render path
``scripts/gen_spine.py`` uses) and diff it against the committed
``docs/capabilities.md`` / ``docs/capabilities.json``. If a connector,
capability, or seam changes without regenerating the artifact, the body diff
fails and tells the dev to run ``scripts/gen_spine.py``.

They pin three properties:

* **drift** — the committed Markdown **body** (provenance header excluded) and
  the full committed JSON match a fresh render of the current code. The JSON is
  compared byte for byte under pydantic >= 2.11, which generates it, and on
  every supported pydantic with five known, equivalent renderings aligned
  (``_KNOWN_RENDERING_DIFFERENCES``);
* **determinism** — rendering twice in one process yields byte-identical
  bodies (no ``frozenset``/``set`` ordering nondeterminism);
* **provenance-insensitivity** — a change confined to the provenance header
  (a different git SHA) does **not** trip the body diff, so the gate cannot
  flap red on every commit.

The body comparison reuses :func:`machina.introspect.render_llms.strip_provenance`
(the exact delimiter the generator chose), never a re-implementation, so the
test and the generator can never disagree about what "the body" is.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pydantic
import pytest

# The committed-artifact paths, taken from the generator itself so the test and
# `scripts/gen_spine.py` can never point at different files.
from scripts.gen_spine import _JSON_PATH, _MD_PATH

from machina.introspect import describe
from machina.introspect.render_llms import (
    render_json,
    render_markdown,
    render_provenance_header,
    strip_provenance,
)

_REGEN_HINT = "Run `python scripts/gen_spine.py` and commit the result."

# `docs/capabilities.json` embeds `MachinaConfig.model_json_schema()` and is
# generated with pydantic >= 2.11.
_REFERENCE_PYDANTIC = (2, 11)
_PYDANTIC = tuple(int(part) for part in pydantic.VERSION.split(".")[:2])

_ABSENT = object()

# The five places where pydantic 2.4-2.10, also supported, render that schema
# differently from the committed file, as (path inside `config_schema`, key,
# value in the committed file, value from pydantic 2.4-2.10; `_ABSENT` when
# that pydantic leaves the key out). JSON Schema reads each pair the same way:
# a missing `additionalProperties` means `true` (the three `dict[str, Any]`
# fields), and `0` and `0.0`, or `2` and `2.0`, are the same number (the
# `temperature` bounds, declared `ge=0, le=2` on a float field). No other
# value, path or type is treated as equivalent.
_KNOWN_RENDERING_DIFFERENCES: tuple[tuple[tuple[str, ...], str, object, object], ...] = (
    (("properties", "logging"), "additionalProperties", True, _ABSENT),
    (
        ("$defs", "ConnectorConfig", "properties", "settings"),
        "additionalProperties",
        True,
        _ABSENT,
    ),
    (("$defs", "ChannelConfig", "properties", "settings"), "additionalProperties", True, _ABSENT),
    (("$defs", "LLMConfig", "properties", "temperature"), "minimum", 0, 0.0),
    (("$defs", "LLMConfig", "properties", "temperature"), "maximum", 2, 2.0),
)


def _render_body() -> str:
    """Render the Markdown body (provenance stripped) from the live core."""
    return strip_provenance(render_markdown(describe(), git_sha="test"))


def _render_json_text() -> str:
    """Render the JSON artifact text from the live core (provenance-free)."""
    return json.dumps(render_json(describe()), indent=2, sort_keys=False) + "\n"


def _comparable_text(artifact: dict[str, Any]) -> str:
    """Render a parsed artifact as text, the five known differences aligned.

    At each `_KNOWN_RENDERING_DIFFERENCES` entry, a value that equals the
    committed file's value *and has its type* (`true`; the integer `0` or `2`)
    is rewritten the way pydantic 2.4-2.10 render it: the key is removed, or the
    integer becomes the float. Any other value there is left as it is. The text
    keeps types and key order, so `2` and `2.0`, or `true` and `1`, still
    differ everywhere else. The artifact is not modified.
    """
    artifact = copy.deepcopy(artifact)
    for path, key, committed, older in _KNOWN_RENDERING_DIFFERENCES:
        node: Any = artifact["config_schema"]
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or key not in node:
            continue
        value = node[key]
        if type(value) is type(committed) and value == committed:
            if older is _ABSENT:
                del node[key]
            else:
                node[key] = older
    return json.dumps(artifact, indent=2, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# Drift — committed artifacts match a fresh render of current code
# ---------------------------------------------------------------------------


def test_committed_markdown_body_matches_fresh_render() -> None:
    """The committed ``capabilities.md`` body is not stale (Markdown drift)."""
    assert _MD_PATH.exists(), (
        f"{_MD_PATH} is missing — generate it with `python scripts/gen_spine.py`."
    )
    committed_body = strip_provenance(_MD_PATH.read_text(encoding="utf-8"))
    assert committed_body == _render_body(), (
        f"docs/capabilities.md body is out of date. {_REGEN_HINT}"
    )


@pytest.mark.skipif(
    _PYDANTIC < _REFERENCE_PYDANTIC,
    reason=(
        f"pydantic {pydantic.VERSION} < 2.11: docs/capabilities.json is generated "
        "with pydantic >= 2.11, and older pydantic renders five values of the "
        "config schema differently but equivalently; "
        "test_committed_json_matches_fresh_render_semantically compares it instead"
    ),
)
def test_committed_json_matches_fresh_render() -> None:
    """The committed ``capabilities.json`` is not stale (JSON drift, byte for byte)."""
    assert _JSON_PATH.exists(), (
        f"{_JSON_PATH} is missing — generate it with `python scripts/gen_spine.py`."
    )
    committed = _JSON_PATH.read_text(encoding="utf-8")
    assert committed == _render_json_text(), (
        f"docs/capabilities.json is out of date. {_REGEN_HINT}"
    )


def test_committed_json_matches_fresh_render_semantically() -> None:
    """The committed ``capabilities.json`` is not stale on any supported pydantic.

    Runs on every pydantic the package accepts, the 2.4 floor included. Both
    sides are aligned only where :func:`_comparable_text` aligns them, so a
    change to a connector, capability, seam, type, required field, default,
    bound or description fails here as it does in the byte-for-byte test.
    """
    assert _JSON_PATH.exists(), (
        f"{_JSON_PATH} is missing — generate it with `python scripts/gen_spine.py`."
    )
    committed = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
    fresh = json.loads(_render_json_text())
    assert _comparable_text(committed) == _comparable_text(fresh), (
        "docs/capabilities.json differs from a fresh render beyond the five known "
        "pydantic < 2.11 renderings. If test_committed_json_matches_fresh_render "
        "passes under pydantic >= 2.11, this pydantic renders something else "
        "differently: add it to _KNOWN_RENDERING_DIFFERENCES. Otherwise run "
        "`python scripts/gen_spine.py` under pydantic >= 2.11 and commit the result."
    )


# ---------------------------------------------------------------------------
# Determinism — two renders in one process are byte-identical
# ---------------------------------------------------------------------------


def test_two_renders_produce_identical_bodies() -> None:
    """Rendering twice in one process yields byte-identical bodies.

    Catches ``set``/``frozenset`` ordering nondeterminism independently of any
    code change: even if the committed artifact were up to date, a flaky sort
    key would surface here.
    """
    assert _render_body() == _render_body()


def test_two_json_renders_are_identical() -> None:
    """Rendering the JSON twice in one process yields identical text."""
    assert _render_json_text() == _render_json_text()


# ---------------------------------------------------------------------------
# Provenance-insensitivity — a SHA-only change does not trip the body diff
# ---------------------------------------------------------------------------


def test_provenance_only_change_does_not_trip_body_diff() -> None:
    """A different provenance header (SHA) leaves the compared body unchanged.

    Simulate a fresh commit by re-rendering the committed Markdown with a new
    provenance header but the same body; the drift comparison (which strips
    provenance) must still see the two as equal, proving the gate ignores the
    SHA.
    """
    committed = _MD_PATH.read_text(encoding="utf-8")
    committed_body = strip_provenance(committed)

    # Swap only the provenance header for one stamped with a different SHA.
    rebranded = render_provenance_header("0" * 40) + "\n" + committed_body

    assert "0" * 40 in rebranded  # the new SHA really is present in the header
    # ...yet the stripped body is identical to the original committed body.
    assert strip_provenance(rebranded) == committed_body
    # ...and identical to a fresh render of the live core.
    assert strip_provenance(rebranded) == _render_body()


# ---------------------------------------------------------------------------
# Gate sensitivity — the comparison detects a real body change
# ---------------------------------------------------------------------------


def test_gate_detects_a_body_mutation_not_masked_by_provenance() -> None:
    """A real body change is caught even when the provenance header is identical.

    The happy-path drift tests only prove committed == fresh. This proves the
    *comparison itself* detects drift: two documents with the SAME provenance
    header but a one-token body difference must compare unequal after stripping
    provenance — so a ``strip_provenance`` bug that collapsed bodies to ``""``
    (making empty == empty) would fail here rather than silently pass the gate.
    """
    body = _render_body()
    mutated = body.replace("BaseConnector", "ZzzRenamedSeam", 1)
    assert mutated != body, "sanity: the token must exist in the rendered body"

    doc_ok = render_provenance_header("abc") + "\n" + body
    doc_bad = render_provenance_header("abc") + "\n" + mutated
    assert strip_provenance(doc_ok) != strip_provenance(doc_bad)


def _node(schema: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Return the object at ``path`` inside a config schema."""
    for part in path:
        schema = schema[part]
    return schema


def test_committed_json_holds_each_known_difference_in_its_reference_form() -> None:
    """Each known difference is where, and as what, the comparison expects it.

    The committed artifact is generated with pydantic >= 2.11, so each
    `_KNOWN_RENDERING_DIFFERENCES` path holds the committed value with its exact
    type (`true`, or an integer). If a schema change moves, removes or changes
    one, this fails rather than leaving the comparison pointed at a value that
    is no longer there.
    """
    schema = json.loads(_JSON_PATH.read_text(encoding="utf-8"))["config_schema"]
    for path, key, committed, _older in _KNOWN_RENDERING_DIFFERENCES:
        value = _node(schema, path).get(key, _ABSENT)
        where = (
            f"{'/'.join((*path, key))}: docs/capabilities.json must be generated "
            "with pydantic >= 2.11, and an entry of _KNOWN_RENDERING_DIFFERENCES "
            "must name a value that file holds"
        )
        assert type(value) is type(committed), where
        assert value == committed, where


_LOGGING = ("properties", "logging")
_TEMPERATURE = ("$defs", "LLMConfig", "properties", "temperature")


@pytest.mark.parametrize(
    ("path", "key", "value", "ignored"),
    [
        # The five known differences, as pydantic 2.4-2.10 render them.
        pytest.param(_LOGGING, "additionalProperties", _ABSENT, True, id="logging-open-dict"),
        pytest.param(
            ("$defs", "ConnectorConfig", "properties", "settings"),
            "additionalProperties",
            _ABSENT,
            True,
            id="connector-settings-open-dict",
        ),
        pytest.param(
            ("$defs", "ChannelConfig", "properties", "settings"),
            "additionalProperties",
            _ABSENT,
            True,
            id="channel-settings-open-dict",
        ),
        pytest.param(_TEMPERATURE, "minimum", 0.0, True, id="temperature-minimum-as-float"),
        pytest.param(_TEMPERATURE, "maximum", 2.0, True, id="temperature-maximum-as-float"),
        # Any other value at those paths.
        pytest.param(_LOGGING, "additionalProperties", False, False, id="logging-closed"),
        pytest.param(_LOGGING, "additionalProperties", 1, False, id="logging-number-for-true"),
        pytest.param(
            _LOGGING,
            "additionalProperties",
            {"type": "string"},
            False,
            id="logging-narrowed-to-str-values",
        ),
        pytest.param(_TEMPERATURE, "minimum", 0.5, False, id="temperature-minimum-changed"),
        pytest.param(_TEMPERATURE, "maximum", 2.5, False, id="temperature-maximum-changed"),
        pytest.param(_TEMPERATURE, "minimum", False, False, id="temperature-minimum-boolean"),
        # Any change anywhere else, including an integer written as a float or
        # a boolean written as a number.
        pytest.param((), "additionalProperties", _ABSENT, False, id="model-extra-allow-dropped"),
        pytest.param(
            ("$defs", "LLMConfig"),
            "additionalProperties",
            _ABSENT,
            False,
            id="nested-model-extra-allow-dropped",
        ),
        pytest.param(_LOGGING, "type", "string", False, id="type-changed"),
        pytest.param((), "required", ["name"], False, id="required-field-added"),
        pytest.param(
            ("$defs", "LLMConfig", "properties"), "temperature", _ABSENT, False, id="removed"
        ),
        pytest.param((), "description", "Changed.", False, id="description-changed"),
        pytest.param(
            ("$defs", "LLMConfig", "properties", "max_tokens"),
            "minimum",
            1.0,
            False,
            id="other-integer-as-float",
        ),
        pytest.param(
            ("properties", "confirmations"), "default", False, False, id="default-flipped"
        ),
        pytest.param(("properties", "confirmations"), "default", 1, False, id="number-for-true"),
        pytest.param(("properties", "sandbox"), "default", 0, False, id="number-for-false"),
    ],
)
def test_semantic_json_comparison_aligns_only_the_five_known_differences(
    path: tuple[str, ...], key: str, value: object, ignored: bool
) -> None:
    """The semantic comparison accepts the five known renderings and nothing else."""
    committed = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
    mutated = copy.deepcopy(committed)
    node = _node(mutated["config_schema"], path)
    if value is _ABSENT:
        del node[key]
    else:
        node[key] = value
    same = _comparable_text(mutated) == _comparable_text(committed)
    assert same is ignored


# ---------------------------------------------------------------------------
# llms.txt copies — repo-root and docs/ copies must stay byte-identical
# ---------------------------------------------------------------------------


def test_llms_txt_copies_stay_in_sync() -> None:
    """The repo-root ``llms.txt`` and ``docs/llms.txt`` must be byte-identical.

    Both ship: the repo-root copy is the conventional ``llms.txt`` location for
    tooling, and ``docs/llms.txt`` is the copy served by the mkdocs site. Nothing
    generates them (``llms.txt`` is hand-curated), so without this guard editing
    one and not the other would silently drift the two copies apart. Paths are
    derived from ``_MD_PATH`` (``docs/capabilities.md``) so the test needs no
    separate notion of the repo layout.
    """
    docs_dir = _MD_PATH.parent  # docs/
    repo_root = docs_dir.parent  # repository root
    root_copy = (repo_root / "llms.txt").read_text(encoding="utf-8")
    docs_copy = (docs_dir / "llms.txt").read_text(encoding="utf-8")
    assert root_copy == docs_copy, (
        "llms.txt and docs/llms.txt have drifted — keep them byte-identical "
        "(edit both, or make one canonical)."
    )
