"""Capability→method-name map and liveness helpers for introspection.

The :class:`~machina.connectors.capabilities.Capability` enum *values* are
stable wire identifiers, **not** method names.  Several connectors back a
capability with a differently-named method — e.g. OPC-UA serves
``subscribe_to_nodes`` via :meth:`OpcUaConnector.subscribe`, and the comms
connectors serve ``receive_message`` via ``listen``.  A naive
``hasattr(cls, capability.value)`` check would therefore wrongly report
those capabilities as missing.

:data:`CAPABILITY_TO_METHOD` is the single source of truth mapping every
:class:`Capability` member to the method that backs it.  The introspection
core emits a capability only when the connector declares it **and** the
mapped method is present **and** the method is not an unimplemented stub
(see :func:`is_stub_method`).
"""

from __future__ import annotations

import inspect
from typing import Any

from machina.connectors.capabilities import Capability

# ---------------------------------------------------------------------------
# Capability → backing method name (single source of truth)
# ---------------------------------------------------------------------------
#
# Verified by direct reads of every connector in ``runtime._CONNECTOR_FACTORIES``
# plus the orphaned ``SimulatedSensorConnector``.  Entries where the method
# name does NOT equal the capability value are flagged with a NOTE so the
# non-obvious mappings are auditable:
#
#   * IoT OPC-UA: subscribe_to_nodes→subscribe, read_node_value→read_value,
#     read_node_values→read_values  (browse_nodes matches).
#   * IoT MQTT:   subscribe_to_topics→subscribe, publish_message→publish.
#   * Comms:      receive_message→listen  (send_message matches).
#   * Calendar:   read_calendar_events→read_events,
#                 create_calendar_event→create_event,
#                 delete_calendar_event→delete_event.
CAPABILITY_TO_METHOD: dict[Capability, str] = {
    # CMMS — core read/write (method name == capability value)
    Capability.READ_ASSETS: "read_assets",
    Capability.READ_WORK_ORDERS: "read_work_orders",
    Capability.GET_WORK_ORDER: "get_work_order",
    Capability.CREATE_WORK_ORDER: "create_work_order",
    Capability.UPDATE_WORK_ORDER: "update_work_order",
    Capability.CLOSE_WORK_ORDER: "close_work_order",
    Capability.CANCEL_WORK_ORDER: "cancel_work_order",
    Capability.READ_SPARE_PARTS: "read_spare_parts",
    Capability.READ_MAINTENANCE_PLANS: "read_maintenance_plans",
    Capability.READ_MAINTENANCE_HISTORY: "read_maintenance_history",
    Capability.READ_FAILURE_MODES: "read_failure_modes",
    # IoT — OPC-UA  (NOTE: names differ from values)
    Capability.SUBSCRIBE_TO_NODES: "subscribe",
    Capability.READ_NODE_VALUE: "read_value",
    Capability.READ_NODE_VALUES: "read_values",
    Capability.BROWSE_NODES: "browse_nodes",
    # IoT — MQTT  (NOTE: names differ from values)
    Capability.SUBSCRIBE_TO_TOPICS: "subscribe",
    Capability.PUBLISH_MESSAGE: "publish",
    # IoT — simulated / sensor history (orphaned: provider not in the registry)
    Capability.GET_RELATED_READINGS: "get_related_readings",
    Capability.GET_LATEST_READING: "get_latest_reading",
    # Document store
    Capability.SEARCH_DOCUMENTS: "search_documents",
    Capability.RETRIEVE_SECTION: "retrieve_section",
    # Communications  (NOTE: receive_message is served by ``listen``)
    Capability.SEND_MESSAGE: "send_message",
    Capability.RECEIVE_MESSAGE: "listen",
    # Calendar  (NOTE: names differ from values)
    Capability.READ_CALENDAR_EVENTS: "read_events",
    Capability.CREATE_CALENDAR_EVENT: "create_event",
    Capability.DELETE_CALENDAR_EVENT: "delete_event",
}


# Marker comment a connector author can place in an unimplemented method body
# so introspection treats it as unwired even when it raises something other
# than ``NotImplementedError`` (e.g. ``GenericSqlConnector.update_work_order``
# raises ``ConnectorError`` with a "not yet implemented" message).
STUB_MARKER = "introspect: stub"


def method_name_for(capability: Capability) -> str | None:
    """Return the method name backing ``capability``, or ``None`` if unmapped.

    A ``None`` result means the capability is missing from
    :data:`CAPABILITY_TO_METHOD` — a coverage gap the introspection core
    surfaces rather than silently dropping.

    Args:
        capability: The capability to resolve.

    Returns:
        The backing method name, or ``None`` when no mapping exists.
    """
    return CAPABILITY_TO_METHOD.get(capability)


def is_stub_method(cls: type[Any], method_name: str) -> bool:
    """Whether ``cls.method_name`` is an unimplemented stub.

    A method is treated as a stub when its source contains a bare
    ``raise NotImplementedError`` or the :data:`STUB_MARKER` comment.  Stub
    methods declare a capability the connector cannot actually serve, so the
    introspection core excludes them ("declared-but-empty is worse than
    undeclared").

    Source inspection failures (C-implemented methods, frozen environments
    without source) degrade to ``False`` — an unreadable method is assumed
    live rather than wrongly suppressed.

    Args:
        cls: The connector class.
        method_name: The method to inspect.

    Returns:
        ``True`` when the method is an unimplemented stub.
    """
    method = getattr(cls, method_name, None)
    if method is None:
        return False
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    if STUB_MARKER in source:
        return True
    # A bare ``raise NotImplementedError`` in the body — detected structurally
    # via the AST, not by scanning raw source, so a docstring or comment that
    # merely mentions the phrase does not false-positive (which would silently
    # drop a live capability from the spine).
    return _body_raises_not_implemented(source)


def _body_raises_not_implemented(source: str) -> bool:
    """Whether a method's source body contains a ``raise NotImplementedError``.

    Parses the source with the :mod:`ast` module and walks the function body
    for a ``raise`` of ``NotImplementedError`` (bare or called). Docstrings and
    comments are not statements, so a mention of the phrase in prose is ignored.
    Falls back to a conservative substring check only if the source cannot be
    parsed (an unusual, defensive case).

    Args:
        source: The method source as returned by :func:`inspect.getsource`.

    Returns:
        ``True`` when the body raises ``NotImplementedError``.
    """
    import ast
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return "raise NotImplementedError" in source
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)),
        None,
    )
    if func is None:
        return False
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
    return False


def has_live_method(cls: type[Any], capability: Capability) -> bool:
    """Whether ``cls`` exposes a live (non-stub) method backing ``capability``.

    Combines the explicit :data:`CAPABILITY_TO_METHOD` lookup with presence
    and non-stub checks.  Returns ``False`` when the capability is unmapped,
    the method is absent, or the method is a stub.

    Args:
        cls: The connector class.
        capability: The capability to verify.

    Returns:
        ``True`` only when a live backing method exists.
    """
    method_name = method_name_for(capability)
    if method_name is None:
        return False
    if not hasattr(cls, method_name):
        return False
    return not is_stub_method(cls, method_name)
