"""The bounded result of a spare-part lookup.

The agent's ``check_spare_parts`` tool and the MCP ``machina_list_spare_parts``
tool both answer with :func:`bounded_spare_parts`. An unfiltered lookup
returns the connector's whole inventory, thousands of parts on a real CMMS:
serializing all of it for the model stalls the event loop and overflows the
model's context. The result therefore carries at most
:data:`SPARE_PARTS_RESULT_LIMIT` parts, the number that matched, and an
explicit flag and note when the list is incomplete, so a partial inventory is
never presented as the whole one.

The limit bounds what is returned, not what is read: the connector still
retrieves every matching part. Narrowing the lookup (by ``sku``) is the way
to a complete answer; there is no paging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from machina.domain.spare_part import SparePart

#: Most spare parts one lookup hands to a model or an MCP client. It also caps
#: the parts listed in the agent's per-turn context
#: (:func:`machina.agent.prompts.format_spare_parts_context`).
SPARE_PARTS_RESULT_LIMIT = 50

_TRUNCATED_NOTE = (
    "Showing {shown} of {total} matching spare parts: the list is incomplete. "
    "Say so if you present it, and do not conclude that a part is missing or out "
    "of stock because it is not listed. Narrow the lookup, e.g. by sku, for a "
    "complete answer."
)


def bounded_spare_parts(
    parts: Sequence[SparePart],
    serialize: Callable[[SparePart], dict[str, Any]],
) -> dict[str, Any]:
    """Build a spare-part lookup result capped at :data:`SPARE_PARTS_RESULT_LIMIT`.

    Returns ``{"parts": [...], "total": n, "truncated": bool}``: ``total``
    counts every part the lookup returned, and ``parts`` holds the first
    :data:`SPARE_PARTS_RESULT_LIMIT` of them in the connector's order. Only
    those are passed to ``serialize``, so the work stays bounded however large
    the inventory. A truncated result also carries a ``note``, which names no
    part, saying the list is incomplete and how to narrow it. ``truncated`` is
    present on every result, so a complete list says so too.

    Args:
        parts: Every part the connector returned for the lookup. Not modified.
        serialize: Turns one part into the dict the caller exposes.

    Returns:
        The result object.
    """
    total = len(parts)
    truncated = total > SPARE_PARTS_RESULT_LIMIT
    result: dict[str, Any] = {
        "parts": [serialize(part) for part in parts[:SPARE_PARTS_RESULT_LIMIT]],
        "total": total,
        "truncated": truncated,
    }
    if truncated:
        result["note"] = _TRUNCATED_NOTE.format(shown=SPARE_PARTS_RESULT_LIMIT, total=total)
    return result
