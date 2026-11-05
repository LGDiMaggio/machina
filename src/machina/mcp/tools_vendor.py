"""Vendor-specific MCP tools — opt-in, non-portable escape hatches.

These tools expose raw vendor APIs (SAP IW38, Maximo attributes, etc.)
and are registered only when ``config.mcp.enable_vendor_tools`` is True.
They are NOT driven by the Capability enum — manually listed here and
registered conditionally in ``build_server``.

**Non-portable**: these tools are tied to a specific CMMS vendor.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import structlog

from machina.connectors.base import set_sandbox_mode
from machina.connectors.cmms._http_errors import rest_errors, status_error, write_outcome_unknown
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

# How each raw write is named in its error messages.
_SAP_ACTION = "create maintenance notification"
_MAXIMO_ACTION = "update resource attributes"

_NOT_JSON_ATTRIBUTES = (
    "Invalid attributes: every value must have a JSON form (NaN and infinity have none), "
    "so the update was not sent"
)


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


def _write_result(
    connector: str, operation: str, resp: Any, *, vendor: str, action: str
) -> dict[str, Any]:
    """Report a raw vendor write's response by the shared REST write contract.

    A 2xx confirms the write: its ``status_code`` and its JSON ``body`` (``{}``
    when empty) are returned. Any other answer is returned as ``status_code``
    and ``error``, never with the body, which can carry the vendor's internal
    URLs and record values: a 4xx is the CMMS refusing the write, and after a
    3xx (redirects are not followed, and 303 See Other can answer an applied
    write) or a 5xx the outcome is unknown, and the error says so. So does a
    2xx whose body is not JSON, which a proxy's login page can be.

    The response is logged as ``vendor_write_completed``, at INFO for a 2xx
    and at WARNING otherwise. The log names the connector, the operation and
    the status only: the body and the request can carry record values and
    credentials.

    Args:
        connector: The connector class name, for the log.
        operation: The tool name, for the log.
        resp: The final httpx response.
        vendor: Label that opens an error message, e.g. ``"Maximo"``.
        action: The write, in an error message, e.g.
            ``"update resource attributes"``.
    """
    status = resp.status_code
    error: ConnectorError | None = None
    body: Any = {}
    if 200 <= status < 300:
        try:
            with rest_errors(vendor, action, write=True):
                body = resp.json() if resp.content else {}
        except ConnectorError as exc:
            error = exc
    else:
        error = status_error(vendor, action, status, write=True)
    (logger.info if error is None else logger.warning)(
        "vendor_write_completed",
        connector=connector,
        operation=operation,
        status_code=status,
    )
    if error is not None:
        return {"status_code": status, "error": str(error)}
    return {"status_code": status, "body": body}


def _write_failure(
    connector: str, operation: str, exc: Exception, *, vendor: str, action: str
) -> dict[str, Any]:
    """Report a raw vendor write that raised, without a foreign exception's text.

    A Machina error's message is written to be shown: the connector helpers
    map transport failures to messages that say whether the write request
    was sent. Any other exception's text can carry the request URL or a
    header value, so only its type is reported and logged; where it was
    raised, before or after the request went out, is not known, so the
    message says the outcome is unknown.
    """
    logger.warning(
        "vendor_write_failed",
        connector=connector,
        operation=operation,
        error_type=type(exc).__name__,
    )
    if isinstance(exc, MachinaError):
        return {"error": str(exc)}
    return {"error": str(write_outcome_unknown(vendor, action, f"raised {type(exc).__name__}"))}


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

    Returns:
        ``status_code`` and the JSON ``body`` of SAP's answer when it is a
        2xx, which confirms the notification was created. Any other answer,
        and any failure, is reported as ``error`` (with ``status_code`` when
        SAP answered), without SAP's response body. After a 4xx, or an error
        that says the write request was not sent, nothing was created. When
        the error says the outcome is unknown (a 3xx, a 5xx, a timeout or a
        broken connection after sending), the notification may have been
        created: check SAP before trying again.
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
            operation=_SAP_ACTION,
        )
    except SandboxViolationError:
        return {
            "description": f"[SANDBOX — no real write performed] {description}",
            "metadata": {"sandbox": True},
        }
    except Exception as exc:
        return _write_failure(
            "SapPmConnector",
            "sap_pm_raw_iw38_notification",
            exc,
            vendor="SAP PM",
            action=_SAP_ACTION,
        )
    return _write_result(
        "SapPmConnector", "sap_pm_raw_iw38_notification", resp, vendor="SAP PM", action=_SAP_ACTION
    )


async def maximo_raw_attribute_update(
    ctx: Context,
    resource_type: str,
    resource_id: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update raw Maximo OSLC attributes on any resource (non-portable).

    This bypasses the domain model and patches arbitrary Maximo
    attributes.  Use only when the domain-level tools are insufficient.
    The update is merged: child objects the attributes do not list are kept,
    and a listed child row without its key is added, so repeating such a
    call adds it again.

    Args:
        resource_type: OSLC object structure (e.g. 'mxwo', 'mxasset'):
            ASCII letters, digits and underscores only.
        resource_id: Rest ID of the resource, the last segment of its OSLC
            href (e.g. '_SElMTFRPUC9XNzc-'); sent as one URL path segment.
            The machina://v1/assets/{asset_id} and
            machina://v1/work-orders/{wo_id} resources of a Maximo record
            carry it as ``metadata['rest_id']``.
        attributes: Dictionary of attribute names to new values.

    Returns:
        ``status_code`` and the JSON ``body`` of Maximo's answer when it is a
        2xx, which confirms the update (a 204 has an empty body). Any other
        answer, and any failure, is reported as ``error`` (with
        ``status_code`` when Maximo answered), without Maximo's response body.
        After a 4xx, or an error that says the write request was not sent,
        the update was not applied. When the error says the outcome is
        unknown (a redirect or another 3xx, a 5xx, a timeout or a broken
        connection after sending), it may have been applied: read the
        resource before trying again.
    """
    from machina.connectors.base import get_sandbox_mode

    # Both values go into the update URL. A value that could address another
    # endpoint is refused before anything else, in sandbox mode too, so a
    # sandbox run answers as a live run would; the connector helper checks
    # again before its request.
    try:
        _oslc_resource_path(resource_type, resource_id)
    except ConnectorError as exc:
        return {"error": str(exc)}
    # httpx would refuse to encode them only when building the request, as an
    # exception the tool could not tell from a failure after sending.
    try:
        json.dumps(attributes, allow_nan=False)
    except (TypeError, ValueError):
        return {"error": _NOT_JSON_ATTRIBUTES}

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
        resp = await conn._patch_resource(
            resource_type, resource_id, attributes or {}, operation=_MAXIMO_ACTION
        )
    except SandboxViolationError:
        return {
            "description": "[SANDBOX — no real write performed] Attribute update logged.",
            "metadata": {"sandbox": True},
        }
    except Exception as exc:
        return _write_failure(
            "MaximoConnector",
            "maximo_raw_attribute_update",
            exc,
            vendor="Maximo",
            action=_MAXIMO_ACTION,
        )
    return _write_result(
        "MaximoConnector",
        "maximo_raw_attribute_update",
        resp,
        vendor="Maximo",
        action=_MAXIMO_ACTION,
    )


VENDOR_TOOLS: list[Callable[..., Any]] = [
    sap_pm_raw_iw38_notification,
    maximo_raw_attribute_update,
]
