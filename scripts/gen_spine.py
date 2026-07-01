"""Regenerate the committed Form-A capability artifacts from the spine.

Calls :func:`machina.introspect.describe`, obtains the current git SHA, renders
the spine via :mod:`machina.introspect.render_llms`, and writes
``docs/capabilities.md`` and ``docs/capabilities.json``.

Two modes:

* (default) **write** — render and overwrite the committed artifacts.
* ``--check`` — render into a buffer and diff the **body** (provenance header
  excluded) of ``docs/capabilities.md`` against the committed file, exiting
  non-zero on drift with a message telling the dev to run the generator. The
  drift gate (``make ci``) uses this mode.

The git call lives here, not in the renderer, so the renderer stays pure and
deterministic. A git failure (no checkout, git absent) degrades to the
``uncommitted`` placeholder rather than aborting.

Run with::

    python scripts/gen_spine.py            # regenerate the artifacts
    python -m scripts.gen_spine --check    # CI drift gate
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from machina.introspect import describe
from machina.introspect.render_llms import (
    UNCOMMITTED_SHA,
    render_json,
    render_markdown,
    strip_provenance,
)

# Repo root = two levels up from this file (scripts/gen_spine.py → repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _REPO_ROOT / "docs"
_MD_PATH = _DOCS / "capabilities.md"
_JSON_PATH = _DOCS / "capabilities.json"


def _git_sha() -> str:
    """Return ``git rev-parse HEAD``, or the placeholder when unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return UNCOMMITTED_SHA
    sha = result.stdout.strip()
    return sha or UNCOMMITTED_SHA


def _render() -> tuple[str, str]:
    """Render the markdown and JSON artifacts (markdown carries provenance)."""
    spine = describe()
    git_sha = _git_sha()
    markdown = render_markdown(spine, git_sha=git_sha)
    payload = json.dumps(render_json(spine), indent=2, sort_keys=False) + "\n"
    return markdown, payload


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (write-tmp-then-rename).

    A rename into place is atomic on the same filesystem, so an interrupted run
    can never leave a half-written or truncated artifact that the drift gate
    would then read as spurious "drift" on the next invocation.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def _write() -> int:
    """Write both artifacts to ``docs/``; returns process exit code 0."""
    markdown, payload = _render()
    _write_atomic(_MD_PATH, markdown)
    _write_atomic(_JSON_PATH, payload)
    print(f"Wrote {_MD_PATH.relative_to(_REPO_ROOT)} and {_JSON_PATH.relative_to(_REPO_ROOT)}")
    return 0


def _check() -> int:
    """Diff freshly-rendered bodies against committed files; 0 = in sync."""
    markdown, payload = _render()
    drift: list[str] = []

    if not _MD_PATH.exists():
        drift.append(f"{_MD_PATH.relative_to(_REPO_ROOT)} is missing")
    else:
        committed = _MD_PATH.read_text(encoding="utf-8")
        # Compare bodies only — the provenance block (git SHA) is excluded so
        # a changed SHA never trips the gate.
        if strip_provenance(committed) != strip_provenance(markdown):
            drift.append(f"{_MD_PATH.relative_to(_REPO_ROOT)} body is out of date")

    if not _JSON_PATH.exists():
        drift.append(f"{_JSON_PATH.relative_to(_REPO_ROOT)} is missing")
    else:
        # The JSON artifact carries no provenance, so a full compare is safe.
        if _JSON_PATH.read_text(encoding="utf-8") != payload:
            drift.append(f"{_JSON_PATH.relative_to(_REPO_ROOT)} is out of date")

    if drift:
        print("Capability artifacts are stale:", file=sys.stderr)
        for item in drift:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\nRun `python scripts/gen_spine.py` and commit the result.",
            file=sys.stderr,
        )
        return 1

    print("Capability artifacts are up to date.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: write the artifacts, or check them with ``--check``."""
    parser = argparse.ArgumentParser(
        prog="gen_spine",
        description="Regenerate (or --check) the committed capability artifacts.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff committed artifacts against a fresh render; exit non-zero on drift.",
    )
    args = parser.parse_args(argv)
    return _check() if args.check else _write()


if __name__ == "__main__":
    raise SystemExit(main())
