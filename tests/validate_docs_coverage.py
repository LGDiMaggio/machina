#!/usr/bin/env python3
"""Validate docs currency: public-API coverage + migration-guide version coverage.

Two presence gates (not content-correctness checks):
  1. Every public symbol is mentioned somewhere in the published docs.
  2. The latest released CHANGELOG version is mentioned in docs/migration/.

The MkDocs ``nav:`` is maintained by hand and the readthedocs site only
renders pages it links to, so a new connector or top-level export can ship
fully undocumented without breaking the (strict) docs build — the page just
never exists. This gate closes that gap: every public symbol must be
mentioned *somewhere* in the published docs.

Public surface checked:
  * everything in ``machina.__all__`` (minus ``__version__``)
  * every concrete ``*Connector`` class under ``src/machina/connectors``
    (protocol/abstract infra in ``_EXEMPT_CONNECTORS`` is excluded —
    those are documented conceptually in connectors/custom.md, not as a
    user-facing connector page)

"Mentioned" means the name appears as a substring in any published
``docs/**/*.md`` (excluding the gitignored ``brainstorms/`` ``plans/``
``solutions/`` planning trees) or in ``mkdocs.yml``. This is intentionally
loose — it enforces "documented at all", not page structure, to stay
low-false-positive. Structure/quality is the job of the write-docs and
sync-docs skills.

Run directly or via pytest::

    python tests/validate_docs_coverage.py
    pytest tests/validate_docs_coverage.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS = REPO_ROOT / "mkdocs.yml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
MIGRATION_DIR = DOCS_DIR / "migration"

# Gitignored, local-only planning trees — not part of the published site.
_LOCAL_DOCS = {"brainstorms", "plans", "solutions"}

# First released version heading in CHANGELOG.md, e.g. "## [0.3.1] - 2026-06-05".
# Skips "## [Unreleased]" because the X.Y.Z group only matches real versions.
_CHANGELOG_VERSION_RE = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)

# Protocol / abstract base connectors — not user-facing connector pages.
# They are covered conceptually in docs/connectors/custom.md.
_EXEMPT_CONNECTORS = {"BaseConnector", "RefreshableConnector"}

_CONNECTOR_CLASS_RE = re.compile(r"^class\s+(\w+Connector)\b", re.MULTILINE)


def _public_api_names() -> set[str]:
    """Top-level public exports from ``machina.__all__`` (minus __version__)."""
    sys.path.insert(0, str(SRC_DIR))
    import machina

    return {n for n in machina.__all__ if n != "__version__"}


def _connector_class_names() -> set[str]:
    """Concrete ``*Connector`` class names declared under connectors/."""
    names: set[str] = set()
    for path in (SRC_DIR / "machina" / "connectors").rglob("*.py"):
        for match in _CONNECTOR_CLASS_RE.finditer(path.read_text(encoding="utf-8")):
            names.add(match.group(1))
    return names - _EXEMPT_CONNECTORS


def _published_docs_blob() -> str:
    parts: list[str] = []
    for path in DOCS_DIR.rglob("*.md"):
        if any(part in _LOCAL_DOCS for part in path.relative_to(DOCS_DIR).parts):
            continue
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    if MKDOCS.exists():
        parts.append(MKDOCS.read_text(encoding="utf-8"))
    return "\n".join(parts)


def find_undocumented() -> list[str]:
    """Return public names with no mention in the published docs."""
    names = _public_api_names() | _connector_class_names()
    blob = _published_docs_blob()
    return sorted(n for n in names if n not in blob)


def latest_changelog_version() -> str | None:
    """Most recent released version in CHANGELOG.md (the first ``[X.Y.Z]``)."""
    if not CHANGELOG.exists():
        return None
    match = _CHANGELOG_VERSION_RE.search(CHANGELOG.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def migration_version_gap() -> str | None:
    """Return the latest released version if it is *not* mentioned in any
    migration guide, else ``None``.

    Enforces version *coverage* of the migration docs — when you cut a release,
    the migration guide must at least acknowledge that version. It does not (and
    cannot) check that the prose is correct; that stays an authoring judgement
    (see the write-docs / sync-docs skills).
    """
    version = latest_changelog_version()
    if version is None or not MIGRATION_DIR.exists():
        return None
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in MIGRATION_DIR.rglob("*.md")
    )
    return None if version in blob else version


def test_public_api_is_documented() -> None:
    """pytest entry point — public surface coverage."""
    missing = find_undocumented()
    assert not missing, (
        "Public API/connectors with no mention in the published docs "
        f"(add a page + mkdocs.yml nav entry, e.g. via /sync-docs): {missing}"
    )


def test_latest_version_in_migration_guide() -> None:
    """pytest entry point — migration-guide version coverage."""
    gap = migration_version_gap()
    assert gap is None, (
        f"CHANGELOG's latest release {gap!r} is not mentioned in any "
        "docs/migration/*.md — add it (at least a 'v{prev} -> v{new}' note) "
        "before release. Prose correctness is your call; this only enforces "
        "that the version is acknowledged."
    )


def main() -> int:
    failed = False

    missing = find_undocumented()
    if missing:
        failed = True
        sys.stderr.write(
            "Undocumented public surface — every public export and concrete "
            "connector must be mentioned in docs/ or mkdocs.yml.\n"
            "Add a page and a nav entry (the /sync-docs skill does this):\n"
        )
        for name in missing:
            sys.stderr.write(f"  - {name}\n")

    gap = migration_version_gap()
    if gap is not None:
        failed = True
        sys.stderr.write(
            f"Migration guide is stale: CHANGELOG's latest release {gap!r} is "
            "not mentioned in docs/migration/. Add at least a version note "
            "before release.\n"
        )

    if failed:
        return 1

    total = len(_public_api_names() | _connector_class_names())
    version = latest_changelog_version() or "?"
    print(
        f"OK: all {total} public names documented; "
        f"migration guide mentions latest release {version}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
