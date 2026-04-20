"""Vendor-specific MCP tools — opt-in, non-portable escape hatches.

These tools expose raw vendor APIs (SAP IW38, Maximo attributes, etc.)
and are registered only when ``config.mcp.enable_vendor_tools`` is True.
They are NOT driven by the Capability enum — manually listed here and
registered conditionally in ``build_server``.

**Non-portable**: these tools are tied to a specific CMMS vendor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import structlog

from machina.exceptions import SandboxViolationError

logger = structlog.get_logger(__name__)


def _runtime(ctx: Any) -> Any:
    return ctx.request_context.lifespan_context["runtime"]


def _find_connector_by_type(runtime: Any, type_prefix: str) -> Any | None:
    """Find the first connector whose class name starts with the given prefix."""
    for _name, conn in runtime.connectors.items():
        if type(conn).__name__.lower().startswith(type_prefix):
            return conn
    return None


async def sap_pm_raw_iw38_notification(
    ctx: Any,
    equipment_id: str,
    notification_type: str = "M2",
    description: str = "",
) -> dict[str, Any]:
    """Create a raw SAP IW38 maintenance notification (non-portable).

    This bypasses the domain model and sends a raw OData payload
    to SAP PM.  Use only when the domain-level tools are insufficient.

    Args:
        equipment_id: SAP equipment number.
        notification_type: SAP notification type (default M2).
        description: Notification description text.
    """
    from machina.connectors.base import get_sandbox_mode

    if get_sandbox_mode():
        logger.info("sandbox_write_blocked", operation="sap_pm_raw_iw38_notification")
        return {
            "description": f"[SANDBOX — no real write performed] {description}",
            "metadata": {"sandbox": True},
        }

    runtime = _runtime(ctx)
    conn = _find_connector_by_type(runtime, "sappm")
    if conn is None:
        return {"error": "No SAP PM connector configured"}
    try:
        payload = {
            "NotificationType": notification_type,
            "Equipment": equipment_id,
            "NotificationText": description,
        }
        resp = await conn._write_with_csrf(
            "POST",
            f"{conn.url}/API_MAINTENANCENOTIFICATION/MaintenanceNotification",
            payload,
        )
        return {"status_code": resp.status_code, "body": resp.json()}
    except SandboxViolationError:
        return {
            "description": f"[SANDBOX — no real write performed] {description}",
            "metadata": {"sandbox": True},
        }
    except Exception as exc:
        return {"error": str(exc)}


async def maximo_raw_attribute_update(
    ctx: Any,
    resource_type: str,
    resource_id: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update raw Maximo OSLC attributes on any resource (non-portable).

    This bypasses the domain model and patches arbitrary Maximo
    attributes.  Use only when the domain-level tools are insufficient.

    Args:
        resource_type: OSLC object structure (e.g. 'mxwo', 'mxasset').
        resource_id: Resource identifier.
        attributes: Dictionary of attribute names to new values.
    """
    from machina.connectors.base import get_sandbox_mode

    if get_sandbox_mode():
        logger.info("sandbox_write_blocked", operation="maximo_raw_attribute_update")
        return {
            "description": "[SANDBOX — no real write performed] Attribute update logged.",
            "metadata": {"sandbox": True},
        }

    runtime = _runtime(ctx)
    conn = _find_connector_by_type(runtime, "maximo")
    if conn is None:
        return {"error": "No Maximo connector configured"}
    attributes = attributes or {}
    try:
        import importlib

        httpx = importlib.import_module("httpx")
        headers = {**conn._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{conn.url}/maximo/oslc/os/{resource_type}/{resource_id}",
                headers=headers,
                json=attributes,
            )
        return {"status_code": resp.status_code, "body": resp.json() if resp.content else {}}
    except SandboxViolationError:
        return {
            "description": "[SANDBOX — no real write performed] Attribute update logged.",
            "metadata": {"sandbox": True},
        }
    except Exception as exc:
        return {"error": str(exc)}


VENDOR_TOOLS: list[Callable[..., Any]] = [
    sap_pm_raw_iw38_notification,
    maximo_raw_attribute_update,
]
