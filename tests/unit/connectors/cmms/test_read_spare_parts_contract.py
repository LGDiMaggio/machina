"""The ``read_spare_parts`` call contract, enforced against every connector.

Machina asks for spare parts from four places: the built-in workflows, the
agent's ``check_spare_parts`` tool, the agent's per-turn context prefetch and
the MCP ``machina_list_spare_parts`` tool. All of them call the connector with
keyword arguments drawn from one contract::

    async def read_spare_parts(self, *, asset_id: str = "", sku: str = "") -> list[SparePart]

Each caller is tested against a test double, which cannot see a real
connector's signature — so a connector that drifts from the contract passes
every caller's tests and then raises ``TypeError`` on every real call (the
workflow step is silently skipped, the agent turn fails). These tests bind each
caller's keyword arguments against the real signature of every registered
connector that declares ``READ_SPARE_PARTS``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from machina.connectors.capabilities import Capability
from machina.introspect.core import (
    _class_base_capabilities,
    _configurable_capabilities,
    _import_class,
)
from machina.runtime import _CONNECTOR_FACTORIES
from machina.workflows import builtins


def _spare_parts_connectors() -> list[tuple[str, type]]:
    """(type, class) for every registered connector that can serve spare parts.

    Includes a connector that only declares the capability under some
    configuration, and lists a class registered under several type aliases once.
    """
    found: dict[type, str] = {}
    for conn_type, dotted_path in sorted(_CONNECTOR_FACTORIES.items()):
        try:
            cls = _import_class(dotted_path)
        except Exception:  # pragma: no cover - import failure tested elsewhere
            continue
        base = _class_base_capabilities(cls, conn_type)
        if Capability.READ_SPARE_PARTS in base | _configurable_capabilities(conn_type, base):
            found.setdefault(cls, conn_type)
    return sorted(((conn_type, cls) for cls, conn_type in found.items()), key=lambda c: c[0])


def _builtin_workflow_calls() -> list[tuple[str, dict[str, Any]]]:
    """The keyword arguments each built-in workflow step passes to read_spare_parts."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for name in builtins.__all__:
        for step in getattr(builtins, name).steps:
            if step.action.rsplit(".", 1)[-1] == Capability.READ_SPARE_PARTS.value:
                calls.append((f"workflow:{name}.{step.name}", dict.fromkeys(step.inputs, "P-201")))
    return calls


# The agent and MCP callers pass fixed keyword sets.
_FIXED_CALLS: list[tuple[str, dict[str, Any]]] = [
    # agent/runtime.py, Agent._execute_tool — the check_spare_parts tool
    ("agent:check_spare_parts", {"asset_id": "P-201", "sku": "SKF-6310"}),
    # agent/runtime.py, Agent._gather_context — the per-turn prefetch
    ("agent:prefetch", {"asset_id": "P-201"}),
    # mcp/tools.py, machina_list_spare_parts
    ("mcp:machina_list_spare_parts", {"asset_id": "P-201"}),
    ("unfiltered", {}),
]

_CONNECTORS = _spare_parts_connectors()
_CALLS = _builtin_workflow_calls() + _FIXED_CALLS


def test_discovery_covers_the_shipped_connectors_and_workflow() -> None:
    """Guard against a vacuous pass: the matrix below must not come up empty."""
    assert {"generic_cmms", "maximo", "sap_pm", "upkeep"} <= {t for t, _ in _CONNECTORS}
    assert "workflow:alarm_to_workorder.check_spare_parts" in {label for label, _ in _CALLS}


@pytest.mark.parametrize(("conn_type", "cls"), _CONNECTORS, ids=[t for t, _ in _CONNECTORS])
@pytest.mark.parametrize(("caller", "kwargs"), _CALLS, ids=[label for label, _ in _CALLS])
def test_read_spare_parts_accepts_every_callers_kwargs(
    conn_type: str, cls: type, caller: str, kwargs: dict[str, Any]
) -> None:
    signature = inspect.signature(cls.read_spare_parts)
    try:
        signature.bind(None, **kwargs)  # None stands in for self
    except TypeError as exc:
        pytest.fail(
            f"{cls.__name__}.read_spare_parts{signature} rejects the {caller} call "
            f"with {sorted(kwargs)}: {exc}"
        )
