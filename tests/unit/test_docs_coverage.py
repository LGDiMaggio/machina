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

# Classes that are intentionally not surfaced in the API reference.  Each
# entry must come with a one-line rationale.  Add liberally — the goal is
# explicit intent, not arbitrary exclusion.  Anything genuinely public-facing
# should get a docs page entry instead of an allowlist line.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # ---- Internal config dataclasses (covered by yaml-config.md prose) -
        "machina.config.schema.ConnectorConfig",
        "machina.config.schema.LLMConfig",
        "machina.config.schema.PlantConfig",
        "machina.config.schema.ChannelConfig",
        "machina.config.schema.McpConfig",
        "machina.config.schema.MachinaConfig",
        # ---- Connector-internal schemas (covered by connector narrative) ---
        "machina.connectors.cmms.generic_schema.FieldSpec",
        "machina.connectors.cmms.generic_schema.ReverseFieldSpec",
        "machina.connectors.cmms.generic_schema.EndpointSpec",
        "machina.connectors.cmms.generic_schema.EntityMapping",
        "machina.connectors.cmms.generic_schema.GenericCmmsYamlConfig",
        "machina.connectors.docs.excel_schema.ColumnMapping",
        "machina.connectors.docs.excel_schema.SheetSchema",
        "machina.connectors.docs.excel_schema.WatcherConfig",
        "machina.connectors.docs.excel_schema.ExcelConnectorConfig",
        "machina.connectors.sql.schema.FieldMapping",
        "machina.connectors.sql.schema.TableMapping",
        "machina.connectors.sql.schema.SqlRetryConfig",
        "machina.connectors.sql.schema.SqlConnectorConfig",
        # ---- Internal protocols / runtime helpers ---------------------------
        "machina.connectors.docs.watcher.RefreshableConnector",  # internal protocol
        "machina.connectors.docs.watcher.FileWatcher",  # internal helper
        "machina.connectors.comms.types.IncomingMessage",  # internal channel protocol
        # ---- Auth helpers (mentioned inline in connector pages) ------------
        "machina.connectors.cmms.auth.OAuth2ClientCredentials",  # constructor mentioned inline
        # ---- IoT internals (configuration types used through MqttConnector) -
        "machina.connectors.iot.mqtt.PayloadFormat",
        "machina.connectors.iot.mqtt.TopicConfig",
        "machina.connectors.iot.mqtt.MqttSubscription",
        "machina.connectors.iot.opcua.SubscriptionConfig",
        "machina.connectors.iot.opcua.Subscription",
        # ---- Internal connectors / channels --------------------------------
        "machina.connectors.iot.simulated.SimulatedSensorConnector",  # test/demo only
        "machina.connectors.comms.telegram.TelegramConnector",  # covered by quickstart
        "machina.connectors.comms.telegram.CliChannel",  # covered by quickstart
        "machina.connectors.docs.document_store.DocumentChunk",  # data shape, returned by search
        "machina.connectors.docs.document_store.DocumentStoreConnector",  # covered by RAG section
        "machina.connectors.docs.excel.ExcelCsvConnector",  # covered by excel.md
        "machina.connectors.sql.generic.GenericSqlConnector",  # covered by sql.md
        # ---- Domain internals ---------------------------------------------
        "machina.domain.calendar.EventType",  # enum used internally by CalendarEvent
        "machina.domain.calendar.CalendarEvent",  # narrative in calendar connector
        "machina.domain.calendar.PlannedDowntime",  # narrative in calendar connector
        "machina.domain.calendar.ShiftPattern",  # narrative in calendar connector
        "machina.domain.services.asset_service.AssetService",  # internal helper, no public API contract
        # ---- MCP internals (covered by docs/mcp/*) ------------------------
        "machina.mcp.auth.StaticBearerTokenVerifier",  # covered by mcp/auth.md narrative
    }
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _public_classes() -> set[str]:
    """Return fully-qualified names of every public class in src/machina.

    A class is public when:

    * Its name does not start with an underscore.
    * It is not defined inside a private module (one whose path component
      starts with ``_``, e.g. ``machina.agent._compat``).
    """
    found: set[str] = set()
    for py_path in sorted(_SRC_DIR.rglob("*.py")):
        rel_parts = py_path.relative_to(
            _SRC_DIR.parent
        ).parts  # ("machina", "agent", "runtime.py")
        # Skip private modules / sub-packages.
        if any(part.startswith("_") and not part.startswith("__") for part in rel_parts):
            continue

        if py_path.name == "__init__.py":
            module = ".".join(rel_parts[:-1])
        elif py_path.name.startswith("__"):
            continue  # dunder modules like __init__.py handled above
        else:
            module = ".".join(rel_parts)[:-3]  # strip .py

        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — defensive
            continue

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                found.add(f"{module}.{node.name}")
    return found


_DOCSTRING_REF = re.compile(r"::: (machina\.[A-Za-z0-9_.]+)")


def _documented_classes() -> set[str]:
    """Return fully-qualified names targeted by ``::: machina.xxx`` directives
    in any markdown file under ``docs/``, excluding gitignored sub-trees.
    """
    found: set[str] = set()
    for md_path in _DOCS_DIR.rglob("*.md"):
        rel_parts = md_path.relative_to(_DOCS_DIR).parts
        if rel_parts and rel_parts[0] in _DOCS_IGNORED_DIRS:
            continue
        for match in _DOCSTRING_REF.finditer(md_path.read_text(encoding="utf-8")):
            found.add(match.group(1))
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDocsCoverage:
    """Drift detection between the framework's public API and its doc site."""

    def test_every_public_class_is_covered_or_allowlisted(self) -> None:
        """Fail when a new public class lands without API-ref coverage.

        A public class is *covered* when it is either:

        * Referenced from a markdown file under ``docs/`` via the
          mkdocstrings directive ``::: machina.module.ClassName``.
        * Explicitly listed in :data:`_ALLOWLIST` above with a rationale.

        Resolution: either add ``::: machina.<module>.<ClassName>`` to an
        appropriate page in ``docs/`` (most commonly under ``docs/api/`` or
        the relevant connector / domain page), or add the class to
        ``_ALLOWLIST`` with a one-line comment explaining why it is not
        meant to be part of the public API reference.
        """
        public = _public_classes()
        documented = _documented_classes()
        missing = (public - documented) - _ALLOWLIST

        if missing:
            lines = ["Public classes missing API-ref coverage in docs/:"]
            for fqn in sorted(missing):
                lines.append(f"  - {fqn}")
            lines.append("")
            lines.append("Resolution: either")
            lines.append("  (a) add `::: <fqn>` to an appropriate page in docs/, or")
            lines.append(
                "  (b) add the class to _ALLOWLIST in this file "
                "with a comment explaining why it is not part of the public API ref."
            )
            pytest.fail("\n".join(lines))

    def test_allowlist_entries_still_exist_in_source(self) -> None:
        """Catch stale allowlist entries — a class removed from src/ should be removed here too."""
        public = _public_classes()
        stale = _ALLOWLIST - public
        if stale:
            lines = [
                "Allowlist entries that no longer exist in src/machina/:",
                *[f"  - {fqn}" for fqn in sorted(stale)],
                "",
                "Resolution: remove these entries from _ALLOWLIST in this file.",
            ]
            pytest.fail("\n".join(lines))

    def test_allowlist_entries_are_not_also_documented(self) -> None:
        """Catch redundancy — an allowlisted class that ALSO has a docs page
        should be dropped from the allowlist (the docs entry is now authoritative).
        """
        documented = _documented_classes()
        redundant = _ALLOWLIST & documented
        if redundant:
            lines = [
                "Allowlisted classes that are ALSO documented in docs/:",
                *[f"  - {fqn}" for fqn in sorted(redundant)],
                "",
                "Resolution: remove these entries from _ALLOWLIST — the docs/ "
                "entry is authoritative.",
            ]
            pytest.fail("\n".join(lines))
