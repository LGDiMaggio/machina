"""Drift gate for the committed Form-A capability artifacts.

These tests make staleness structurally impossible: they regenerate the
artifact from the **live** core (the same render path
``scripts/gen_spine.py`` uses) and diff it against the committed
``docs/capabilities.md`` / ``docs/capabilities.json``. If a connector,
capability, or seam changes without regenerating the artifact, the body diff
fails and tells the dev to run ``scripts/gen_spine.py``.

They pin three properties:

* **drift** — the committed Markdown **body** (provenance header excluded) and
  the full committed JSON match a fresh render of the current code;
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

import json

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


def _render_body() -> str:
    """Render the Markdown body (provenance stripped) from the live core."""
    return strip_provenance(render_markdown(describe(), git_sha="test"))


def _render_json_text() -> str:
    """Render the JSON artifact text from the live core (provenance-free)."""
    return json.dumps(render_json(describe()), indent=2, sort_keys=False) + "\n"


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


def test_committed_json_matches_fresh_render() -> None:
    """The committed ``capabilities.json`` is not stale (JSON drift)."""
    assert _JSON_PATH.exists(), (
        f"{_JSON_PATH} is missing — generate it with `python scripts/gen_spine.py`."
    )
    committed = _JSON_PATH.read_text(encoding="utf-8")
    assert committed == _render_json_text(), (
        f"docs/capabilities.json is out of date. {_REGEN_HINT}"
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
