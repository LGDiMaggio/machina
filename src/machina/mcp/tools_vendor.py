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

from machina.connectors.base import set_sandbox_mode
from machina.connectors.cmms.maximo import _oslc_resource_path
from machina.exceptions import ConnectorError, MachinaError, SandboxViolationError

# See machina.mcp.tools: FastMCP injects the request context only into a
# parameter annotated with its bare ``Context`` class (opaque to mypy).
if TYPE_CHECKING:
    Context = Any
else:
    try:
        from mcp.server.fastmcp import Context
    except ImportError:  # pragma: no cover - the MCP SDK is required to register tools
        Context = Any

logger = structlog.get_logger(__name__)


def _runtime(ctx: Any) -> Any:
    """Return the runtime and re-establish sandbox mode for this request.

    Identical to ``mcp.tools._runtime``: each MCP tool call runs in its own
    request task that does not inherit the ``_sandbox_mode`` contextvar set
    once at lifespan startup. The sandbox short-circuit in every vendor tool
    reads ``get_sandbox_mode()``, and the ``@sandbox_aware`` connector helper
    each tool writes through reads the same contextvar, so it MUST be
    funnelled through here first — otherwise a server started in sandbox mode
    reports sandbox as off and the raw vendor write executes live.
    """
    runtime = ctx.request_context.lifespan_context["runtime"]
    set_sandbox_mode(runtime.sandbox_mode)
    return runtime


def _find_connector_by_type(runtime: Any, type_prefix: str) -> Any | None:
    """Find the first connector whose class name starts with the given prefix."""
    for _name, conn in runtime.connectors.items():
        if type(conn).__name__.lower().startswith(type_prefix):
            return conn
    return None


def _write_result(connector: str, operation: str, resp: Any) -> dict[str, Any]:
    """Report a raw vendor write's response: its status, and its body if JSON.

    A body that is not JSON (an empty success, a proxy's error page) is
    reported as ``{}``, never as an error: the write may have been applied,
    and the status code says what happened.

    The response is logged at INFO for a 2xx status and at WARNING for any
    other (a vendor refusal, a gateway error, a redirect to a login page),
    since the write was most likely not applied. The log names the connector,
    the operation and the status only: the body and the request can carry
    record values and credentials.
    """
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    log = logger.info if 200 <= resp.status_code < 300 else logger.warning
    log(
        "vendor_write_completed",
        connector=connector,
        operation=operation,
        status_code=resp.status_code,
    )
    return {"status_code": resp.status_code, "body": body}


def _write_failure(connector: str, operation: str, exc: Exception) -> dict[str, Any]:
    """Report a raw vendor write that raised, without a foreign exception's text.

    A Machina error's message is written to be shown. Any other exception's
    text (httpx, h11) can carry the request URL or a header value, such as an
    API key with a stray newline that h11 refuses to send, so only its type
    is reported and logged.
    """
    logger.warning(
        "vendor_write_failed",
        connector=connector,
        operation=operation,
        error_type=type(exc).__name__,
    )
    if isinstance(exc, MachinaError):
        return {"error": str(exc)}
    return {"error": f"{operation} failed: {type(exc).__name__}"}


async def sap_pm_raw_iw38_notification(
    ctx: Context,
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

    # _runtime re-establishes the sandbox contextvar for this request task;
    # it must run before get_sandbox_mode() is read.
    runtime = _runtime(ctx)
    if get_sandbox_mode():
        logger.info("sandbox_write_blocked", operation="sap_pm_raw_iw38_notification")
        return {
            "description": f"[SANDBOX — no real write performed] {description}",
            "metadata": {"sandbox": True},
        }

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
    except SandboxViolationError:
        return {
            "description": f"[SANDBOX — no real write performed] {description}",
            "metadata": {"sandbox": True},
        }
    except Exception as exc:
        return _write_failure("SapPmConnector", "sap_pm_raw_iw38_notification", exc)
    return _write_result("SapPmConnector", "sap_pm_raw_iw38_notification", resp)


async def maximo_raw_attribute_update(
    ctx: Context,
    resource_type: str,
    resource_id: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update raw Maximo OSLC attributes on any resource (non-portable).

    This bypasses the domain model and patches arbitrary Maximo
    attributes.  Use only when the domain-level tools are insufficient.
    The update is merged: child objects the attributes do not list are kept.

    Args:
        resource_type: OSLC object structure (e.g. 'mxwo', 'mxasset'):
            ASCII letters, digits and underscores only.
        resource_id: Rest ID of the resource, the last segment of its OSLC
            href (e.g. '_QkVERk9SRC9XQzY-'); sent as one URL path segment.
        attributes: Dictionary of attribute names to new values.
    """
    from machina.connectors.base import get_sandbox_mode

    # Both values go into the PATCH URL. A value that could address another
    # endpoint is refused before anything else, in sandbox mode too, so a
    # sandbox run answers as a live run would; the connector helper checks
    # again before its request.
    try:
        _oslc_resource_path(resource_type, resource_id)
    except ConnectorError as exc:
        return {"error": str(exc)}

    # _runtime re-establishes the sandbox contextvar for this request task;
    # it must run before get_sandbox_mode() is read.
    runtime = _runtime(ctx)
    if get_sandbox_mode():
        logger.info("sandbox_write_blocked", operation="maximo_raw_attribute_update")
        return {
            "description": "[SANDBOX — no real write performed] Attribute update logged.",
            "metadata": {"sandbox": True},
        }

    conn = _find_connector_by_type(runtime, "maximo")
    if conn is None:
        return {"error": "No Maximo connector configured"}
    try:
        resp = await conn._patch_resource(resource_type, resource_id, attributes or {})
    except SandboxViolationError:
        return {
            "description": "[SANDBOX — no real write performed] Attribute update logged.",
            "metadata": {"sandbox": True},
        }
    except Exception as exc:
        return _write_failure("MaximoConnector", "maximo_raw_attribute_update", exc)
    return _write_result("MaximoConnector", "maximo_raw_attribute_update", resp)


VENDOR_TOOLS: list[Callable[..., Any]] = [
    sap_pm_raw_iw38_notification,
    maximo_raw_attribute_update,
]
