#!/usr/bin/env python3
"""Validate Python code blocks in Markdown documentation.

Extracts all ``python`` fenced code blocks from .md files, parses
them with :mod:`ast`, and verifies that ``from machina ...`` imports
resolve against the installed package.

Run directly or via pytest::

    python tests/validate_doc_snippets.py
    pytest tests/validate_doc_snippets.py -v
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Markdown files to scan (relative to repo root)
MD_DIRS = [REPO_ROOT, REPO_ROOT / "docs", REPO_ROOT / "examples"]

# Blocks containing these markers are aspirational / not-yet-runnable
SKIP_MARKERS = {"v0.3", "coming soon", "planned", "not yet implemented"}

# Code blocks that are pseudo-code or comparison tables (not real Python)
PSEUDO_CODE_MARKERS = {"-->", "..."}


def find_md_files() -> list[Path]:
    """Glob all Markdown files in the repo."""
    files: list[Path] = []
    for d in MD_DIRS:
        if d == REPO_ROOT:
            files.extend(d.glob("*.md"))
        else:
            files.extend(d.rglob("*.md"))
    return sorted(set(files))


_CODE_BLOCK_RE = re.compile(
    r"^```python\s*\n(.*?)^```",
    re.MULTILINE | re.DOTALL,
)


def extract_python_blocks(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, code)`` for each Python fenced block."""
    blocks: list[tuple[int, str]] = []
    for m in _CODE_BLOCK_RE.finditer(text):
        line = text[: m.start()].count("\n") + 1
        blocks.append((line, m.group(1)))
    return blocks


def should_skip(code: str) -> bool:
    """Return True if the block is aspirational or pseudo-code."""
    lower = code.lower()
    for marker in SKIP_MARKERS:
        if marker in lower:
            return True
    return any(marker in code for marker in PSEUDO_CODE_MARKERS)


def check_syntax(code: str, filepath: str, line: int) -> str | None:
    """Return an error string if the code has a syntax error."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as exc:
        return f"{filepath}:{line + (exc.lineno or 0)}: SyntaxError: {exc.msg}"


def extract_imports(code: str) -> list[str]:
    """Return the top-level module for each ``from machina...`` import."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("machina"):
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("machina"):
                    modules.append(alias.name)
    return modules


def check_import(module: str) -> str | None:
    """Return an error string if the module cannot be found."""
    spec = importlib.util.find_spec(module)
    if spec is None:
        return f"ImportError: No module named '{module}'"
    return None


def validate_all() -> list[str]:
    """Run all validations and return a list of error strings."""
    errors: list[str] = []
    for md_path in find_md_files():
        text = md_path.read_text(encoding="utf-8", errors="replace")
        rel = md_path.relative_to(REPO_ROOT)
        for line, code in extract_python_blocks(text):
            if should_skip(code):
                continue
            err = check_syntax(code, str(rel), line)
            if err:
                errors.append(err)
                continue
            for mod in extract_imports(code):
                err = check_import(mod)
                if err:
                    errors.append(f"{rel}:{line}: {err}")
    return errors


# -- pytest integration -----------------------------------------------


def test_doc_snippets_valid() -> None:
    """All Python blocks in Markdown docs must parse and have valid imports."""
    errors = validate_all()
    if errors:
        msg = "Documentation snippet validation failed:\n" + "\n".join(f"  {e}" for e in errors)
        raise AssertionError(msg)


# -- standalone -------------------------------------------------------

if __name__ == "__main__":
    errs = validate_all()
    if errs:
        print("FAIL — documentation snippet errors:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print("OK — all documentation snippets valid")
