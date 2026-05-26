"""Drift detection — every public class in ``src/machina/`` must be either
documented via an ``::: machina.xxx.ClassName`` mkdocstrings directive in
``docs/`` or explicitly listed in the allowlist below with rationale.

This test exists to make the documentation system self-maintaining: when a
contributor adds a new public class, this test fails until they either add an
API-reference page entry or justify the omission in the allowlist.  The
docstring on the class itself is enough — mkdocstrings pulls it automatically
into the site at build time.  The drift detection here lives at the
*coverage* layer (does the class have any API-ref entry at all), not the
content layer (is the docstring rich enough — that's a human-review concern).

Files under ``docs/plans/``, ``docs/brainstorms/`` and ``docs/solutions/`` are
gitignored and excluded from the scan; coverage that lives only in those
directories does NOT count.

Run as part of ``pytest tests/unit`` — also catches regressions where a
docs page is moved or the ``:::`` directive is accidentally deleted.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src" / "machina"
_DOCS_DIR = _REPO_ROOT / "docs"

# Directories under docs/ that are gitignored — their content is NOT
# considered when computing coverage, because it never reaches the published
# site or the CI build.
_DOCS_IGNORED_DIRS = frozenset({"plans", "brainstorms", "solutions"})

# Classes intentionally NOT surfaced via mkdocstrings ``:::`` directives.
# Each entry maps the fully-qualified class name to a rationale string.
# Storing as a dict (rather than a frozenset with inline comments) means the
# rationale travels with the FQN into the failure message — a contributor
# who sees a test failure is shown *why* an entry exists, without having to
# read this file to find the matching source comment.
#
# Add liberally — the goal is explicit intent, not arbitrary exclusion.
# Anything genuinely public-facing should get a docs page entry instead of
# an allowlist line.  If the rationale starts to feel weak, that is the
# signal the class deserves documentation.
_ALLOWLIST: dict[str, str] = {
    # ---- Internal config dataclasses (covered by yaml-config.md prose) -----
    "machina.config.schema.ConnectorConfig": "YAML schema dataclass; user-facing surface is yaml-config.md.",
    "machina.config.schema.LLMConfig": "YAML schema dataclass; user-facing surface is yaml-config.md.",
    "machina.config.schema.PlantConfig": "YAML schema dataclass; user-facing surface is yaml-config.md.",
    "machina.config.schema.ChannelConfig": "YAML schema dataclass; user-facing surface is yaml-config.md.",
    "machina.config.schema.McpConfig": "YAML schema dataclass; user-facing surface is yaml-config.md.",
    "machina.config.schema.MachinaConfig": "YAML schema dataclass; user-facing surface is yaml-config.md.",
    # ---- Connector-internal schemas (covered by connector narrative) -------
    "machina.connectors.cmms.generic_schema.FieldSpec": "YAML field mapping dataclass; surface is connectors/generic_cmms.md.",
    "machina.connectors.cmms.generic_schema.ReverseFieldSpec": "YAML reverse-mapping dataclass; surface is connectors/generic_cmms.md.",
    "machina.connectors.cmms.generic_schema.EndpointSpec": "YAML endpoint dataclass; surface is connectors/generic_cmms.md.",
    "machina.connectors.cmms.generic_schema.EntityMapping": "YAML entity mapping; surface is connectors/generic_cmms.md.",
    "machina.connectors.cmms.generic_schema.GenericCmmsYamlConfig": "YAML root config; surface is connectors/generic_cmms.md.",
    "machina.connectors.docs.excel_schema.ColumnMapping": "YAML column mapping; surface is connectors/excel.md.",
    "machina.connectors.docs.excel_schema.SheetSchema": "YAML sheet schema; surface is connectors/excel.md.",
    "machina.connectors.docs.excel_schema.WatcherConfig": "File-watcher config; surface is connectors/excel.md.",
    "machina.connectors.docs.excel_schema.ExcelConnectorConfig": "YAML root config; surface is connectors/excel.md.",
    "machina.connectors.sql.schema.FieldMapping": "YAML field mapping; surface is connectors/sql.md.",
    "machina.connectors.sql.schema.TableMapping": "YAML table mapping; surface is connectors/sql.md.",
    "machina.connectors.sql.schema.SqlRetryConfig": "Retry policy dataclass; surface is connectors/sql.md.",
    "machina.connectors.sql.schema.SqlConnectorConfig": "YAML root config; surface is connectors/sql.md.",
    # ---- Internal protocols / runtime helpers -----------------------------
    "machina.connectors.docs.watcher.RefreshableConnector": "Internal protocol; not part of public connector API.",
    "machina.connectors.docs.watcher.FileWatcher": "Internal helper for file-modified callbacks.",
    # ---- Auth helpers (mentioned inline in connector pages) ---------------
    "machina.connectors.cmms.auth.OAuth2ClientCredentials": "Auth scheme; documented inline in connectors/sap-pm.md examples.",
    # ---- IoT internals (configuration types used through MqttConnector) ----
    "machina.connectors.iot.mqtt.PayloadFormat": "Subscription enum; surface is connectors/mqtt.md.",
    "machina.connectors.iot.mqtt.TopicConfig": "Subscription config; surface is connectors/mqtt.md.",
    "machina.connectors.iot.mqtt.MqttSubscription": "Internal subscription record; surface is connectors/mqtt.md.",
    "machina.connectors.iot.opcua.SubscriptionConfig": "Subscription config; surface is connectors/opcua.md.",
    "machina.connectors.iot.opcua.Subscription": "Internal subscription record; surface is connectors/opcua.md.",
    # ---- Domain calendar entities (covered by connectors/calendar.md) -----
    "machina.domain.calendar.EventType": "Enum used internally by CalendarEvent.",
    "machina.domain.calendar.CalendarEvent": "Narrative coverage in connectors/calendar.md is sufficient.",
    "machina.domain.calendar.PlannedDowntime": "Narrative coverage in connectors/calendar.md is sufficient.",
    "machina.domain.calendar.ShiftPattern": "Narrative coverage in connectors/calendar.md is sufficient.",
    # ---- Connectors / channels with narrative-only coverage ---------------
    # These have dedicated docs/connectors/*.md pages; surfacing the class
    # via ::: would duplicate without adding signal.  Wave B of the
    # report-luigi review revisits whether SimulatedSensorConnector,
    # DocumentStoreConnector, DocumentChunk, and IncomingMessage should be
    # promoted to API ref — for now they sit here with explicit rationale.
    "machina.connectors.iot.simulated.SimulatedSensorConnector": "Demo / prototyping connector; narrative coverage planned in docs/connectors/iot-simulated.md.",
    "machina.connectors.comms.telegram.TelegramConnector": "Covered by quickstart and examples; narrative pattern, not API ref.",
    "machina.connectors.comms.telegram.CliChannel": "Covered by quickstart and examples; narrative pattern, not API ref.",
    "machina.connectors.comms.types.IncomingMessage": "Channel protocol type; documented inline in docs/api/agent.md channel section.",
    "machina.connectors.docs.document_store.DocumentChunk": "Search result data shape; surface is connectors/custom.md + RAG narrative.",
    "machina.connectors.docs.document_store.DocumentStoreConnector": "Covered by connectors/custom.md RAG section narrative.",
    "machina.connectors.docs.excel.ExcelCsvConnector": "Covered by connectors/excel.md narrative.",
    "machina.connectors.sql.generic.GenericSqlConnector": "Covered by connectors/sql.md narrative.",
    # ---- Other internals ---------------------------------------------------
    "machina.domain.services.asset_service.AssetService": "Internal helper; no public API contract.",
    "machina.mcp.auth.StaticBearerTokenVerifier": "Implementation detail; surface is mcp/auth.md narrative.",
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


# ``::: <fqn>`` — mkdocstrings directive form used in markdown.  Permits
# any whitespace (or none) between the colons and the symbol so the gate
# stays honest if a contributor writes ``:::foo`` or ``:::  foo`` — the
# rendered site treats those as equivalent.  Restricted to FQNs anchored
# at ``machina.`` so unrelated cross-references in narrative prose do not
# accidentally count as "documented".
_MKDOCS_DIRECTIVE_RE = re.compile(r":::\s*(machina\.[A-Za-z0-9_.]+)")

# Fenced code-block delimiters (``` or ~~~).  We strip fenced sections
# before matching directives so that a tutorial showing a ``:::`` example
# *inside* a code block does not falsely count as real coverage at site
# build time.
_FENCE_RE = re.compile(r"^(```|~~~).*?$", re.MULTILINE)


def _strip_fenced_blocks(text: str) -> str:
    """Return *text* with fenced markdown code blocks removed.

    Naive fence pairing: every fence start consumes lines until the next
    matching fence on its own line.  Sufficient for the drift gate — the
    aim is to prevent ``:::`` directives inside code examples from being
    counted as real documentation; a contributor pathological enough to
    have unmatched fences in their docs is going to fail the mkdocs build
    long before this gate matters.
    """
    out_lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out_lines.append(line)
    return "\n".join(out_lines)


@cache
def _public_classes() -> frozenset[str]:
    """Return fully-qualified names of every public class in ``src/machina``.

    A class is public when:

    * Its name does not start with an underscore.
    * It is not defined inside a private module (one whose path component
      starts with a single ``_``, e.g. ``machina.agent._compat``).
    * It is a *top-level* class — nested classes defined inside another
      class or function are excluded by design (rare in this codebase,
      and not generally meant as primary API surface).

    Caching means a full mkdocs build's worth of test methods only walks
    the source tree once per pytest run.
    """
    found: set[str] = set()
    for py_path in sorted(_SRC_DIR.rglob("*.py")):
        rel_parts = py_path.relative_to(_SRC_DIR.parent).parts
        # Skip private modules / sub-packages.  Single-underscore prefix
        # (``_compat``, ``_google``) marks internal; double-underscore
        # (``__init__``, ``__pycache__``) is *not* private.
        if any(part.startswith("_") and not part.startswith("__") for part in rel_parts):
            continue

        if py_path.name == "__init__.py":
            module = ".".join(rel_parts[:-1])
        elif py_path.name.startswith("__"):
            # Other dunder files (``__main__.py``, ``__version__.py``);
            # not module-namespace contributors for API-ref purposes.
            continue
        else:
            module = ".".join(rel_parts)[:-3]  # strip ``.py``

        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — defensive
            continue

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                found.add(f"{module}.{node.name}")
    return frozenset(found)


@cache
def _documented_classes() -> frozenset[str]:
    """Return fully-qualified names targeted by ``::: machina.xxx`` directives
    in any markdown file under ``docs/``, excluding gitignored sub-trees
    (``plans``, ``brainstorms``, ``solutions``) and directive references
    that live inside fenced code blocks (which are tutorial / example text,
    not real mkdocstrings invocations).
    """
    found: set[str] = set()
    for md_path in _DOCS_DIR.rglob("*.md"):
        rel_parts = md_path.relative_to(_DOCS_DIR).parts
        if rel_parts and rel_parts[0] in _DOCS_IGNORED_DIRS:
            continue
        body = _strip_fenced_blocks(md_path.read_text(encoding="utf-8"))
        for match in _MKDOCS_DIRECTIVE_RE.finditer(body):
            found.add(match.group(1))
    return frozenset(found)


def _format_undocumented_failure(missing: set[str] | frozenset[str]) -> str:
    return "\n".join(
        [
            "Public classes missing API-ref coverage in docs/:",
            *[f"  - {fqn}" for fqn in sorted(missing)],
            "",
            "Resolution: either",
            "  (a) add `::: <fqn>` to an appropriate page in docs/, or",
            "  (b) add the class to _ALLOWLIST in this file with a rationale "
            "explaining why it is not part of the public API ref.",
        ]
    )


def _format_stale_failure(stale: set[str] | frozenset[str]) -> str:
    return "\n".join(
        [
            "Allowlist entries that no longer exist in src/machina/:",
            *[f"  - {fqn}  ({_ALLOWLIST[fqn]})" for fqn in sorted(stale)],
            "",
            "Resolution: remove these entries from _ALLOWLIST in this file.",
        ]
    )


def _format_redundant_failure(redundant: set[str] | frozenset[str]) -> str:
    return "\n".join(
        [
            "Allowlisted classes that are ALSO documented in docs/:",
            *[f"  - {fqn}  ({_ALLOWLIST[fqn]})" for fqn in sorted(redundant)],
            "",
            "Resolution: remove these entries from _ALLOWLIST — the docs/ entry is authoritative.",
        ]
    )


# ---------------------------------------------------------------------------
# Tests — drift detection on the real codebase
# ---------------------------------------------------------------------------


class TestDocsCoverage:
    """Drift detection between the framework's public API and its doc site."""

    def test_every_public_class_is_covered_or_allowlisted(self) -> None:
        """Fail when a new public class lands without API-ref coverage.

        A public class is *covered* when it is either:

        * Referenced from a markdown file under ``docs/`` via the
          mkdocstrings directive ``::: machina.module.ClassName``.
        * Explicitly listed in :data:`_ALLOWLIST` with a rationale.

        Resolution: either add ``::: <fqn>`` to an appropriate page in
        ``docs/`` (most commonly under ``docs/api/`` or the relevant
        connector / domain page), or add the class to ``_ALLOWLIST``
        with a rationale explaining why it is not meant to be part of
        the public API reference.
        """
        missing = (_public_classes() - _documented_classes()) - set(_ALLOWLIST)
        if missing:
            pytest.fail(_format_undocumented_failure(missing))

    def test_allowlist_entries_still_exist_in_source(self) -> None:
        """A class removed from src/ should also be removed from the allowlist."""
        stale = set(_ALLOWLIST) - _public_classes()
        if stale:
            pytest.fail(_format_stale_failure(stale))

    def test_allowlist_entries_are_not_also_documented(self) -> None:
        """An allowlisted class that ALSO has a docs page should be dropped from the allowlist.

        The docs entry is the more authoritative signal; the allowlist
        entry then becomes redundant maintenance overhead.
        """
        redundant = set(_ALLOWLIST) & _documented_classes()
        if redundant:
            pytest.fail(_format_redundant_failure(redundant))


# ---------------------------------------------------------------------------
# Tests — failure-path branches and helper internals
#
# The three tests above pass on a clean codebase, which means their
# ``pytest.fail`` bodies (~30 lines each) are dead code on every successful
# CI run.  A mutation that replaced ``pytest.fail`` with ``pass`` would
# survive undetected.  These unit tests exercise the failure-path branches
# directly so the gate's "loud failure" promise is itself tested.
# ---------------------------------------------------------------------------


class TestFailurePathFormatting:
    """The three pytest.fail bodies are themselves under test."""

    def test_undocumented_failure_lists_missing_with_resolution_hint(self) -> None:
        msg = _format_undocumented_failure({"machina.foo.Bar", "machina.baz.Qux"})
        assert "machina.foo.Bar" in msg
        assert "machina.baz.Qux" in msg
        # Sorted order
        assert msg.index("machina.baz.Qux") < msg.index("machina.foo.Bar")
        # Resolution hint present and actionable
        assert "add `::: <fqn>`" in msg
        assert "_ALLOWLIST" in msg

    def test_stale_failure_includes_rationale(self) -> None:
        # Use a real allowlist entry so _ALLOWLIST[fqn] resolves.
        sample = next(iter(_ALLOWLIST))
        msg = _format_stale_failure({sample})
        assert sample in msg
        # Rationale must travel into the failure message — that's the
        # whole point of promoting _ALLOWLIST from frozenset to dict.
        assert _ALLOWLIST[sample] in msg
        assert "remove these entries from _ALLOWLIST" in msg

    def test_redundant_failure_includes_rationale(self) -> None:
        sample = next(iter(_ALLOWLIST))
        msg = _format_redundant_failure({sample})
        assert sample in msg
        assert _ALLOWLIST[sample] in msg
        assert "the docs/ entry is authoritative" in msg


class TestHelpers:
    """Edge-case coverage for the discovery helpers."""

    def test_strip_fenced_blocks_removes_backtick_fences(self) -> None:
        text = (
            "Real ::: machina.real.Class here.\n"
            "```python\n"
            "::: machina.fake.FakeClass  # NOT a real directive\n"
            "```\n"
            "And ::: machina.also_real.AlsoReal too."
        )
        stripped = _strip_fenced_blocks(text)
        assert "machina.real.Class" in stripped
        assert "machina.also_real.AlsoReal" in stripped
        assert "machina.fake.FakeClass" not in stripped

    def test_strip_fenced_blocks_removes_tilde_fences(self) -> None:
        text = "Before\n~~~\n::: machina.fake.Hidden\n~~~\nAfter ::: machina.shown.Real"
        stripped = _strip_fenced_blocks(text)
        assert "machina.fake.Hidden" not in stripped
        assert "machina.shown.Real" in stripped

    def test_documented_classes_ignores_fenced_directives(self) -> None:
        """``_documented_classes`` must not count ``:::`` inside fenced examples."""
        # Sanity-check the helper composition; the per-doc behaviour is
        # exercised end-to-end by the test_every_public_class test.  Here
        # we just confirm the contract: fence-stripped input filters out
        # the fake.
        candidates = set()
        for match in _MKDOCS_DIRECTIVE_RE.finditer(
            _strip_fenced_blocks("```\n::: machina.fake.X\n```\n::: machina.real.Y\n")
        ):
            candidates.add(match.group(1))
        assert candidates == {"machina.real.Y"}

    def test_directive_regex_accepts_canonical_and_tolerant_spacing(self) -> None:
        """The mkdocs site renders both ``::: foo`` and ``:::foo``; the gate must too."""
        for variant in (
            ":::machina.no_space.A",
            "::: machina.one_space.B",
            ":::   machina.three_spaces.C",
            "::: machina.tab_space.D",
        ):
            match = _MKDOCS_DIRECTIVE_RE.search(variant)
            assert match is not None, f"Variant did not match: {variant!r}"
            assert match.group(1).startswith("machina.")

    def test_public_classes_skips_private_modules(self) -> None:
        """Single-underscore sub-modules (e.g. ``_compat``, ``_google``) must
        not contribute classes to the public set.
        """
        public = _public_classes()
        # _google.py defines GoogleCalendarBackend — must be absent.
        assert not any(fqn.endswith("._google.GoogleCalendarBackend") for fqn in public)
        # _compat.py is a private module — any class in it must be absent.
        assert not any("._compat." in fqn for fqn in public)

    def test_public_classes_is_cached(self) -> None:
        """The cache annotation lets multiple test methods share one walk."""
        assert _public_classes() is _public_classes()
