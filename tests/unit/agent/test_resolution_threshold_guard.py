"""Structural guard: the resolution-confidence verdict has one derivation site.

The entity-resolution confidence verdict used to be re-derived independently in
several places — the runtime's commit gate and the prompt renderer each ran their
own ``< RESOLUTION_MIN_CONFIDENCE`` comparison and could silently disagree (the
renderer's low-confidence nudge could not fire on a 1.0 tie while the gate
withheld). That was collapsed onto one source of truth,
:func:`machina.agent.entity_resolver.resolution_verdict` (and the private
``_band_for`` it delegates to), which both consumers now read.

Nothing about that collapse *prevents a fourth consumer from reintroducing a
local threshold comparison* and diverging again. This test is that prevention. It
is the same shape as the spine drift gate (``tests/unit/introspect/test_drift.py``
+ ``make ci``'s ``spine-check``): a cheap, committed invariant that fails CI the
moment the property it protects is violated.

**The invariant.** Outside ``entity_resolver.py``, no code in ``src/machina/``
may *compare* a value against one of the resolution threshold constants
(``RESOLUTION_MIN_CONFIDENCE``, ``RESOLUTION_HIGH_CONFIDENCE``, and any future
``RESOLUTION_*_CONFIDENCE`` sibling). A consumer that needs the verdict must route
through :func:`resolution_verdict` / ``_ResolutionVerdict`` / ``_band_for`` — the
one place allowed to turn a raw confidence into an authority decision.

**Why an AST walk rather than a text grep.** The check must not be so broad it
fires on unrelated float comparisons, so it keys on the *named constants*, not on
bare ``0.4`` / ``0.7`` literals (which appear all over the codebase for unrelated
reasons). But those constant names also appear, by design, in ``runtime.py``'s
docstrings as ``:data:`RESOLUTION_MIN_CONFIDENCE``` prose — the repo's house style
is heavily cross-referenced docstrings. A regex keyed on "operator near the
constant name" would false-positive the moment a docstring described the
comparison (e.g. "commits when confidence >= RESOLUTION_HIGH_CONFIDENCE"). Walking
:class:`ast.Compare` nodes is exactly as cheap — it parses each file once, no name
resolution or typing — and is structurally blind to comments and docstrings,
which are never comparison nodes. It is a lint-shaped invariant, not a semantic
analysis.

The two-accessor pattern is safe by construction: ``_tool_search_assets`` in
``runtime.py`` deliberately surfaces raw ``r.confidence`` values *ungated* for
lenient display. It performs no comparison against the thresholds, so it is not an
``ast.Compare`` against the constants and this guard never flags it. A dedicated
test below pins that.

Paths and the guarded constant set are both derived from the
``entity_resolver`` module object itself, so this test and the code under guard
can never point at a different file or a stale list of constants.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from machina.agent import entity_resolver

# The single site allowed to compare against the thresholds, and the package root
# to scan — both taken from the module object so they cannot drift apart or point
# at a different install than the one whose constants we guard.
_ALLOWED_SITE = Path(entity_resolver.__file__).resolve()
_MACHINA_PKG = _ALLOWED_SITE.parent.parent  # .../src/machina

# The guarded constants, discovered from the module rather than hardcoded: any
# ``RESOLUTION_*_CONFIDENCE`` added later is protected automatically, the same way
# the drift gate reads its paths from the generator.
_THRESHOLD_NAME_RE = re.compile(r"^RESOLUTION_[A-Z0-9_]*CONFIDENCE$")
_THRESHOLD_CONSTANTS = frozenset(
    name for name in vars(entity_resolver) if _THRESHOLD_NAME_RE.match(name)
)


def _referenced_name(node: ast.expr) -> str | None:
    """The identifier a comparison operand refers to, if it is a plain reference.

    Handles both a bare name (``RESOLUTION_MIN_CONFIDENCE``) and a module-qualified
    access (``er.RESOLUTION_MIN_CONFIDENCE``); anything else (a call, a literal, a
    subscript) has no single referent and returns ``None``.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _threshold_comparison_hits(source: str, filename: str = "<snippet>") -> list[tuple[int, str]]:
    """Every direct comparison against a guarded threshold constant in ``source``.

    Returns ``(lineno, constant_name)`` for each operand of an :class:`ast.Compare`
    that references one of :data:`_THRESHOLD_CONSTANTS`. A chained comparison
    (``MIN <= x < HIGH``) contributes one hit per referenced constant. Docstrings
    and comments are not comparison nodes, so prose that merely names a constant
    is never reported.
    """
    tree = ast.parse(source, filename=filename)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for operand in (node.left, *node.comparators):
            name = _referenced_name(operand)
            if name in _THRESHOLD_CONSTANTS:
                hits.append((node.lineno, name))
    return hits


def _guarded_files() -> list[Path]:
    """Every ``.py`` under ``src/machina`` except the one allowed derivation site."""
    return [p for p in _MACHINA_PKG.rglob("*.py") if p.resolve() != _ALLOWED_SITE]


# ---------------------------------------------------------------------------
# Sensitivity — the checker actually detects a threshold comparison
# ---------------------------------------------------------------------------


def test_checker_flags_a_direct_threshold_comparison() -> None:
    """A bare ``confidence >= RESOLUTION_HIGH_CONFIDENCE`` is reported."""
    source = "def commits(confidence):\n    return confidence >= RESOLUTION_HIGH_CONFIDENCE\n"
    assert [name for _, name in _threshold_comparison_hits(source)] == [
        "RESOLUTION_HIGH_CONFIDENCE"
    ]


def test_checker_flags_a_module_qualified_comparison() -> None:
    """A module-qualified ``er.RESOLUTION_MIN_CONFIDENCE`` comparison is reported too."""
    source = "flag = value < er.RESOLUTION_MIN_CONFIDENCE\n"
    assert [name for _, name in _threshold_comparison_hits(source)] == [
        "RESOLUTION_MIN_CONFIDENCE"
    ]


def test_checker_flags_a_chained_comparison() -> None:
    """A chained ``MIN <= x < HIGH`` reports both referenced constants."""
    source = "band = RESOLUTION_MIN_CONFIDENCE <= x < RESOLUTION_HIGH_CONFIDENCE\n"
    assert {name for _, name in _threshold_comparison_hits(source)} == _THRESHOLD_CONSTANTS


# ---------------------------------------------------------------------------
# Specificity — the checker does NOT fire on the things it must leave alone
# ---------------------------------------------------------------------------


def test_checker_ignores_docstring_and_comment_mentions() -> None:
    """Prose that names a constant — the shape runtime.py already ships — is not a hit.

    This is the whole reason the guard walks the AST instead of grepping text: the
    constant appears in ``:data:`` docstring references and explanatory comments,
    and neither is a comparison.
    """
    source = (
        '"""Refused below :data:`RESOLUTION_MIN_CONFIDENCE` — see the write gate."""\n'
        "# A local `value < RESOLUTION_MIN_CONFIDENCE` here would re-derive the verdict.\n"
        "x = 1\n"
    )
    assert _threshold_comparison_hits(source) == []


def test_checker_ignores_ungated_confidence_surfacing() -> None:
    """The two-accessor lenient-display path surfaces raw scores and must not trip.

    ``_tool_search_assets`` returns ``r.confidence`` verbatim with no threshold
    comparison; strict gating happens elsewhere through the verdict.
    """
    source = (
        "def _tool_search_assets(resolved):\n"
        "    return [{'confidence': r.confidence} for r in resolved[:5]]\n"
    )
    assert _threshold_comparison_hits(source) == []


def test_checker_ignores_unrelated_float_comparisons() -> None:
    """Unrelated float comparisons — including bare 0.4/0.7 literals — are not hits."""
    source = (
        "if stock_quantity > 0:\n    pass\nif score < 0.4:\n    pass\nif ratio >= 0.7:\n    pass\n"
    )
    assert _threshold_comparison_hits(source) == []


# ---------------------------------------------------------------------------
# The live invariant — no threshold comparison outside the allowed site
# ---------------------------------------------------------------------------


def test_no_threshold_comparison_outside_entity_resolver() -> None:
    """No file under ``src/machina`` (bar ``entity_resolver.py``) compares thresholds."""
    offenders: list[str] = []
    for path in _guarded_files():
        for lineno, name in _threshold_comparison_hits(
            path.read_text(encoding="utf-8"), str(path)
        ):
            offenders.append(
                f"  {path.relative_to(_MACHINA_PKG.parent)}:{lineno} — compares {name}"
            )
    assert not offenders, (
        "Resolution-threshold comparison found outside entity_resolver.py:\n"
        + "\n".join(offenders)
        + "\n\nDo not re-derive the resolution verdict. Route the decision through "
        "resolution_verdict() / _ResolutionVerdict / _band_for() in "
        "machina.agent.entity_resolver — the single source of truth."
    )


# ---------------------------------------------------------------------------
# Sanity — the guard guards something, and the exemption is load-bearing
# ---------------------------------------------------------------------------


def test_threshold_constants_were_discovered() -> None:
    """The guarded set is non-empty — a guard that guards nothing is broken."""
    assert {"RESOLUTION_MIN_CONFIDENCE", "RESOLUTION_HIGH_CONFIDENCE"} <= _THRESHOLD_CONSTANTS


def test_entity_resolver_is_the_load_bearing_exemption() -> None:
    """The exempted file really does compare against every guarded constant.

    Proves the checker finds real comparisons in real code (not just snippets) and
    that exempting ``entity_resolver.py`` is meaningful — it is the site the guard
    exists to permit, not a carve-out for a file that never compares. Mirrors the
    drift gate's "the token must exist" sanity check.
    """
    hits = _threshold_comparison_hits(
        _ALLOWED_SITE.read_text(encoding="utf-8"), str(_ALLOWED_SITE)
    )
    assert {name for _, name in hits} == _THRESHOLD_CONSTANTS
    assert _ALLOWED_SITE not in {p.resolve() for p in _guarded_files()}
