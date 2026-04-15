#!/usr/bin/env python3
"""Validate that every example script parses, imports, and constructs.

Three layers of check — each catches a different class of regression:

1. **Syntax.** :func:`ast.parse` every ``examples/**/*.py`` file.
2. **Imports resolve.** Every ``from machina …`` import in each file is
   locatable via :func:`importlib.util.find_spec`. Catches the case where
   a connector or entity is renamed or removed without updating the
   examples.
3. **Module executes.** Every ``examples/<dir>/agent.py`` is imported so
   its top-level statements (``agent = Agent(...)``, ``workflow = ...``,
   helper-function definitions with type annotations that import from
   ``machina``, etc.) actually run. When the module exposes a
   module-level ``agent`` global that is a machina ``Agent``, the check
   also asserts that type — for examples that build their agent inside
   a function, import-only is the coverage we get. Catches the
   "imports fine but blows up at first call" bug class that produced the
   post-v0.2.0 reactive fix cadence.

Run directly or via pytest::

    python tests/validate_examples.py
    pytest tests/validate_examples.py -v
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

# Example directories that intentionally have no runnable `agent.py`
# (placeholder READMEs for planned features). Keep this list empty by
# default — adding an entry is an explicit decision that a demo is not
# expected to be runnable yet.
_SKIP_CONSTRUCT_DIRS: set[str] = {"05_multi_agent_team"}


def find_example_scripts() -> list[Path]:
    """Return all Python files under ``examples/``."""
    return sorted(EXAMPLES_DIR.rglob("*.py"))


def find_runnable_agent_modules() -> list[Path]:
    """Return every ``examples/<dir>/agent.py`` that should be constructible."""
    return sorted(
        path
        for path in EXAMPLES_DIR.glob("*/agent.py")
        if path.parent.name not in _SKIP_CONSTRUCT_DIRS
    )


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


def check_module_constructs(agent_path: Path) -> str | None:
    """Import the example module and verify its ``agent`` global is an ``Agent``.

    This executes the module body, which is where ``agent = Agent(...)``
    runs. If Agent's constructor or any connector's constructor raises,
    we see it here — not in production when a user runs the example.
    """
    from machina.agent.runtime import Agent  # local import: machina must be installed

    rel = agent_path.relative_to(REPO_ROOT)
    example_dir = agent_path.parent
    # Each example manipulates sys.path relative to its own location
    # (e.g. `_preflight.py` lives one directory up). Match that layout.
    dir_paths = [str(example_dir.parent), str(example_dir)]
    added = [p for p in dir_paths if p not in sys.path]
    sys.path[:0] = added
    module_name = f"_machina_example_{example_dir.name}"
    prev_module = sys.modules.pop(module_name, None)

    try:
        spec = importlib.util.spec_from_file_location(module_name, agent_path)
        if spec is None or spec.loader is None:
            return f"{rel}: could not load module spec"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            return f"{rel}: module body raised {type(exc).__name__}: {exc}"

        agent_obj = getattr(module, "agent", None)
        # Examples that construct lazily (inside build_agent() or main())
        # don't expose a module-level global. The import itself already
        # exercised every top-level statement, so that's the regression
        # guard — we don't require the `agent` global for those.
        if agent_obj is not None and not isinstance(agent_obj, Agent):
            return (
                f"{rel}: module-level `agent` is "
                f"{type(agent_obj).__name__}, expected machina.Agent"
            )
        return None
    finally:
        sys.modules.pop(module_name, None)
        if prev_module is not None:
            sys.modules[module_name] = prev_module
        for p in added:
            if p in sys.path:
                sys.path.remove(p)


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

    for agent_path in find_runnable_agent_modules():
        err = check_module_constructs(agent_path)
        if err:
            errors.append(err)

    return errors


# -- pytest integration -----------------------------------------------


def test_example_scripts_valid() -> None:
    """All example scripts must parse, imports resolve, and Agent(...) construct."""
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
