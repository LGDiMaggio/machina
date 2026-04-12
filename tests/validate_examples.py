#!/usr/bin/env python3
"""Validate that all example scripts parse and their imports resolve.

Scans ``examples/**/*.py``, verifies each file's syntax via
:func:`ast.parse`, and checks that every ``from machina …`` import
can be found in the installed package.

Run directly or via pytest::

    python tests/validate_examples.py
    pytest tests/validate_examples.py -v
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


def find_example_scripts() -> list[Path]:
    """Return all Python files under ``examples/``."""
    return sorted(EXAMPLES_DIR.rglob("*.py"))


def check_syntax(filepath: Path) -> str | None:
    """Return an error string if the file has a syntax error."""
    try:
        source = filepath.read_text(encoding="utf-8")
        ast.parse(source, filename=str(filepath))
        return None
    except SyntaxError as exc:
        return f"{filepath.relative_to(REPO_ROOT)}:{exc.lineno}: SyntaxError: {exc.msg}"


def extract_machina_imports(filepath: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, module_path)`` for all ``from machina …`` imports."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("machina"):
            results.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("machina"):
                    results.append((node.lineno, alias.name))
    return results


def check_import(module: str) -> str | None:
    """Return an error string if the module cannot be found."""
    spec = importlib.util.find_spec(module)
    if spec is None:
        return f"ImportError: No module named '{module}'"
    return None


def validate_all() -> list[str]:
    """Run all validations and return a list of error strings."""
    errors: list[str] = []
    for script in find_example_scripts():
        rel = script.relative_to(REPO_ROOT)
        err = check_syntax(script)
        if err:
            errors.append(err)
            continue
        for lineno, module in extract_machina_imports(script):
            err = check_import(module)
            if err:
                errors.append(f"{rel}:{lineno}: {err}")
    return errors


# -- pytest integration -----------------------------------------------


def test_example_scripts_valid() -> None:
    """All example scripts must parse and have resolvable machina imports."""
    errors = validate_all()
    if errors:
        msg = "Example script validation failed:\n" + "\n".join(f"  {e}" for e in errors)
        raise AssertionError(msg)


# -- standalone -------------------------------------------------------

if __name__ == "__main__":
    errs = validate_all()
    if errs:
        print("FAIL — example script errors:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print("OK — all example scripts valid")
