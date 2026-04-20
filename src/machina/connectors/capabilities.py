"""Capability enum — typed identifiers for connector actions.

Connectors declare a :class:`frozenset` of :class:`Capability` values so
the agent, workflow engine, and MCP server can discover at runtime what
actions each connector supports.  ``Capability`` is a ``StrEnum``: the
enum value is the literal wire-format string used in configuration files
and JSON payloads.  Migrating from opaque ``list[str]`` to this enum
catches typos at import time and enables deterministic auto-registration
of MCP tools from connector capabilities.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """Actions a connector can perform, declared at registration.

    Membership in a connector's ``capabilities`` frozenset grants
    permission for the agent/MCP layer to invoke the corresponding
    method.  String values are stable wire identifiers — do not change
    them without a deprecation cycle.
    """

    # CMMS — core read/write
    READ_ASSETS = "read_assets"
    READ_WORK_ORDERS = "read_work_orders"
    GET_WORK_ORDER = "get_work_order"
    CREATE_WORK_ORDER = "create_work_order"
    UPDATE_WORK_ORDER = "update_work_order"
    CLOSE_WORK_ORDER = "close_work_order"
    CANCEL_WORK_ORDER = "cancel_work_order"
    READ_SPARE_PARTS = "read_spare_parts"
    READ_MAINTENANCE_PLANS = "read_maintenance_plans"
    READ_MAINTENANCE_HISTORY = "read_maintenance_history"

    # IoT — OPC-UA
    SUBSCRIBE_TO_NODES = "subscribe_to_nodes"
    READ_NODE_VALUE = "read_node_value"
    READ_NODE_VALUES = "read_node_values"
    BROWSE_NODES = "browse_nodes"

    # IoT — MQTT
    SUBSCRIBE_TO_TOPICS = "subscribe_to_topics"
    PUBLISH_MESSAGE = "publish_message"

    # IoT — simulated / sensor history
    GET_RELATED_READINGS = "get_related_readings"
    GET_LATEST_READING = "get_latest_reading"

    # Document store
    SEARCH_DOCUMENTS = "search_documents"
    RETRIEVE_SECTION = "retrieve_section"

    # Communications
    SEND_MESSAGE = "send_message"
    RECEIVE_MESSAGE = "receive_message"

    # Calendar
    READ_CALENDAR_EVENTS = "read_calendar_events"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    DELETE_CALENDAR_EVENT = "delete_calendar_event"
