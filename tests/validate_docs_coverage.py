#!/usr/bin/env python3
"""Validate that the public API surface is mentioned in the published docs.

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

# Gitignored, local-only planning trees — not part of the published site.
_LOCAL_DOCS = {"brainstorms", "plans", "solutions"}

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


def test_public_api_is_documented() -> None:
    """pytest entry point."""
    missing = find_undocumented()
    assert not missing, (
        "Public API/connectors with no mention in the published docs "
        f"(add a page + mkdocs.yml nav entry, e.g. via /sync-docs): {missing}"
    )


def main() -> int:
    missing = find_undocumented()
    if missing:
        sys.stderr.write(
            "Undocumented public surface — every public export and concrete "
            "connector must be mentioned in docs/ or mkdocs.yml.\n"
            "Add a page and a nav entry (the /sync-docs skill does this):\n"
        )
        for name in missing:
            sys.stderr.write(f"  - {name}\n")
        return 1
    total = len(_public_api_names() | _connector_class_names())
    print(f"OK: all {total} public names are mentioned in the docs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
