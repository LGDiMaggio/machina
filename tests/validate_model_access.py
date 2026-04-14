#!/usr/bin/env python3
"""Validate that examples and builtins access only real model attributes.

Parses Python files with :mod:`ast`, finds attribute accesses on known
domain/workflow objects (``result.xxx``, ``asset.xxx``, ``sr.xxx``, …),
and checks that each attribute actually exists on the corresponding
class.

This prevents the class of bug where a model field is renamed in
``src/`` but the examples still use the old name — which only surfaces
at runtime.

Run directly or via pytest::

    python tests/validate_model_access.py
    pytest tests/validate_model_access.py -v
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── Model classes to validate against ─────────────────────────────

from machina.domain.alarm import Alarm  # noqa: E402
from machina.domain.asset import Asset  # noqa: E402
from machina.domain.failure_mode import FailureMode  # noqa: E402
from machina.domain.spare_part import SparePart  # noqa: E402
from machina.domain.work_order import WorkOrder  # noqa: E402
from machina.workflows.models import (  # noqa: E402
    Step,
    StepResult,
    Workflow,
    WorkflowResult,
)


def _get_attrs(cls: type) -> set[str]:
    """Return all public attribute names on a class (fields + properties + methods)."""
    attrs: set[str] = set()

    # dataclass fields
    with contextlib.suppress(TypeError):
        attrs.update(f.name for f in dc_fields(cls))

    # pydantic fields
    if hasattr(cls, "model_fields"):
        attrs.update(cls.model_fields.keys())

    # properties and methods
    for name, _obj in inspect.getmembers(cls):
        if not name.startswith("_"):
            attrs.add(name)

    return attrs


# Map variable names (as used in examples) → class to validate against.
# When a file does ``for sr in result.steps:``, ``sr`` is a StepResult.
# When it does ``result = await agent.trigger_workflow(…)``, ``result``
# is a WorkflowResult.
_VAR_TO_CLASS: dict[str, type] = {
    # Workflow layer
    "result": WorkflowResult,
    "sr": StepResult,
    "step": Step,
    "workflow": Workflow,
    "wf": Workflow,
    # Domain layer
    "asset": Asset,
    "alarm": Alarm,
    "wo": WorkOrder,
    "work_order": WorkOrder,
    "part": SparePart,
    "spare": SparePart,
    "fm": FailureMode,
    "failure_mode": FailureMode,
}


# ── AST scan ──────────────────────────────────────────────────────


def _extract_attr_accesses(filepath: Path) -> list[tuple[int, str, str]]:
    """Return ``(lineno, var_name, attr_name)`` for known variables."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            var = node.value.id
            if var in _VAR_TO_CLASS:
                results.append((node.lineno, var, node.attr))
    return results


def _files_to_scan() -> list[Path]:
    """Return example and builtin-workflow Python files."""
    files: list[Path] = []
    files.extend((REPO_ROOT / "examples").rglob("*.py"))
    builtins = SRC_DIR / "machina" / "workflows" / "builtins"
    if builtins.exists():
        files.extend(builtins.rglob("*.py"))
    return sorted(f for f in files if f.name != "__init__.py")


# ── Validation ────────────────────────────────────────────────────


def validate_all() -> list[str]:
    """Check every attribute access and return error strings."""
    errors: list[str] = []
    for filepath in _files_to_scan():
        rel = filepath.relative_to(REPO_ROOT)
        for lineno, var, attr in _extract_attr_accesses(filepath):
            cls = _VAR_TO_CLASS[var]
            valid_attrs = _get_attrs(cls)
            if attr not in valid_attrs:
                errors.append(
                    f"{rel}:{lineno}: {var}.{attr} — "
                    f"'{attr}' does not exist on {cls.__name__}. "
                    f"Did you mean one of: {sorted(a for a in valid_attrs if not a.startswith('_'))[:8]}?"
                )
    return errors


# ── pytest integration ────────────────────────────────────────────


def test_model_attribute_access() -> None:
    """All attribute accesses on known model objects must resolve."""
    errors = validate_all()
    if errors:
        msg = "Model attribute access validation failed:\n" + "\n".join(f"  {e}" for e in errors)
        raise AssertionError(msg)


# ── standalone ────────────────────────────────────────────────────

if __name__ == "__main__":
    errs = validate_all()
    if errs:
        print("FAIL — model attribute access errors:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    print(f"OK — all model attribute accesses valid ({len(_files_to_scan())} files scanned)")
