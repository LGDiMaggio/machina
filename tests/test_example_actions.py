"""Validate that every workflow action string in examples and builtins resolves.

Imports each workflow, collects ``Step.action`` values, and verifies
each one maps to a known connector capability, domain service method,
or special action (``agent.reason``, ``channels.send_message``).

Run via pytest::

    pytest tests/test_example_actions.py -v
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# Ensure machina is importable
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# -- Helpers -----------------------------------------------------------


def _collect_connector_capabilities() -> set[str]:
    """Introspect all connector classes and return their declared capabilities."""
    caps: set[str] = set()

    connector_packages = [
        "machina.connectors.cmms",
        "machina.connectors.iot",
        "machina.connectors.comms",
        "machina.connectors.docs",
    ]

    for pkg_name in connector_packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        for attr_name in dir(pkg):
            obj = getattr(pkg, attr_name)
            if not inspect.isclass(obj):
                continue

            # Check for ClassVar list (direct attribute on the class dict)
            raw_caps = obj.__dict__.get("capabilities")
            if isinstance(raw_caps, (list, tuple)):
                caps.update(raw_caps)

            # Also check _BASE_CAPABILITIES for GenericCmmsConnector
            base_caps = obj.__dict__.get("_BASE_CAPABILITIES")
            if isinstance(base_caps, (list, tuple)):
                caps.update(base_caps)
            optional_caps = obj.__dict__.get("_OPTIONAL_CAPABILITIES")
            if isinstance(optional_caps, dict):
                caps.update(optional_caps.keys())
    return caps


def _collect_domain_service_methods() -> dict[str, set[str]]:
    """Return a mapping of service_name -> set of method names."""
    # Map service prefix -> (module_path, class_name)
    service_classes: dict[str, tuple[str, str]] = {
        "failure_analyzer": ("machina.domain.services.failure_analyzer", "FailureAnalyzer"),
        "work_order_factory": ("machina.domain.services.work_order_factory", "WorkOrderFactory"),
        "maintenance_scheduler": (
            "machina.domain.services.maintenance_scheduler",
            "MaintenanceScheduler",
        ),
        "domain": ("machina.domain.services.asset_service", "AssetService"),
    }

    services: dict[str, set[str]] = {}
    for svc_name, (mod_name, cls_name) in service_classes.items():
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            continue
        methods = {
            m for m in dir(cls) if not m.startswith("_") and callable(getattr(cls, m, None))
        }
        services[svc_name] = methods
    return services


# Special actions handled by the workflow engine directly
SPECIAL_ACTIONS = {"agent.reason", "channels.send_message"}

# Actions that require connectors not yet built (documented as future)
FUTURE_ACTIONS = {"erp.create_purchase_order"}


def _validate_action(
    action: str,
    connector_caps: set[str],
    service_methods: dict[str, set[str]],
) -> str | None:
    """Return an error string if the action cannot be resolved."""
    if not action:
        return None

    if action in SPECIAL_ACTIONS:
        return None

    if action in FUTURE_ACTIONS:
        return None

    parts = action.split(".", 1)
    if len(parts) != 2:
        return f"Invalid action format (expected 'prefix.method'): {action!r}"

    prefix, method = parts

    # Check domain services
    if prefix in service_methods:
        if method in service_methods[prefix]:
            return None
        return (
            f"Service '{prefix}' has no method '{method}'. "
            f"Available: {sorted(service_methods[prefix])}"
        )

    # Check connector capabilities
    if method in connector_caps:
        return None

    return (
        f"No connector capability '{method}' and no service '{prefix}' found for action {action!r}"
    )


def _extract_actions_from_file(filepath: Path) -> list[tuple[int, str]]:
    """Parse a Python file and extract ``action=`` values from Step() calls."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Look for Step(..., action="...")
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "Step":
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "action"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    results.append((node.lineno, kw.value.value))
    return results


# -- Fixtures ----------------------------------------------------------


@pytest.fixture(scope="module")
def connector_capabilities() -> set[str]:
    return _collect_connector_capabilities()


@pytest.fixture(scope="module")
def service_methods() -> dict[str, set[str]]:
    return _collect_domain_service_methods()


# -- Tests -------------------------------------------------------------


def _all_workflow_files() -> list[Path]:
    """Return all Python files that might contain workflow definitions."""
    files: list[Path] = []
    # Example scripts
    files.extend((REPO_ROOT / "examples").rglob("*.py"))
    # Builtin workflows
    builtins = SRC_DIR / "machina" / "workflows" / "builtins"
    if builtins.exists():
        files.extend(builtins.rglob("*.py"))
    return sorted(set(f for f in files if f.name != "__init__.py"))


def _collect_all_actions() -> list[tuple[str, int, str]]:
    """Collect all (filepath, lineno, action) tuples from workflow files."""
    all_actions: list[tuple[str, int, str]] = []
    for fp in _all_workflow_files():
        for lineno, action in _extract_actions_from_file(fp):
            all_actions.append((str(fp.relative_to(REPO_ROOT)), lineno, action))
    return all_actions


_ALL_ACTIONS = _collect_all_actions()


@pytest.mark.parametrize(
    "filepath,lineno,action",
    _ALL_ACTIONS,
    ids=[f"{fp}:{ln}:{act}" for fp, ln, act in _ALL_ACTIONS],
)
def test_action_resolves(
    filepath: str,
    lineno: int,
    action: str,
    connector_capabilities: set[str],
    service_methods: dict[str, set[str]],
) -> None:
    """Every workflow action string must map to a real capability or service."""
    error = _validate_action(action, connector_capabilities, service_methods)
    if error:
        pytest.fail(f"{filepath}:{lineno}: {error}")
