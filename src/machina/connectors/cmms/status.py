"""Work-order status rules shared by the vendor CMMS connectors.

A ``read_work_orders`` status filter reaches SAP PM, Maximo and UpKeep as the
:class:`WorkOrderStatus` enum from code, but as text from LLM and MCP-client
input (``"closed"``), and a caller may also pass a vendor status code
(``"REL"``, ``"WMATL"``, ``"onHold"``). :func:`status_filter` tells the two
apart. A status update must not report success when the vendor did not apply
the status: :func:`status_not_applied` builds the error for that case.
"""

from __future__ import annotations

import structlog

from machina.domain.work_order import WorkOrderStatus
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


def status_filter(status: WorkOrderStatus | str) -> WorkOrderStatus | None:
    """Return a status filter as a :class:`WorkOrderStatus`, or ``None`` for a vendor code.

    The enum, or text equal to one of its values in any case and without
    surrounding spaces (``"Closed"``), is a Machina status. Any other text is a vendor status code. None of the
    codes the SAP PM, Maximo and UpKeep mappers know is spelled like a
    Machina value (``CLSD``, ``CLOSE`` and ``complete`` are not ``closed``).
    """
    if isinstance(status, WorkOrderStatus):
        return status
    try:
        return WorkOrderStatus(status.strip().lower())
    except ValueError:
        return None


def status_not_applied(
    connector: str, work_order_id: str, wanted: str, actual: str
) -> ConnectorError:
    """Log, and return the error for, a status update the vendor accepted but did not apply.

    Args:
        connector: The connector class name, for the log.
        work_order_id: The work order that was updated.
        wanted: The status the update asked for.
        actual: The status the work order reads back with.

    Returns:
        A :class:`ConnectorError` to raise. Its message says the status was
        not applied and that the work order should be read before the update
        is repeated, since the update's other fields may have been applied.
    """
    logger.warning(
        "work_order_status_not_applied",
        connector=connector,
        operation="update_work_order",
        work_order_id=work_order_id,
        wanted_status=wanted,
        actual_status=actual,
    )
    return ConnectorError(
        f"The CMMS accepted the update of work order {work_order_id}, but its status reads "
        f"back as {actual!r}, not {wanted!r}: the status change was not applied. Read the "
        "work order before repeating the update."
    )
