"""URL-path helper shared by the CMMS REST connectors."""

from __future__ import annotations

from urllib.parse import quote

from machina.exceptions import ConnectorError


def path_segment(record_id: object) -> str:
    """Encode a caller-supplied ID as exactly one URL path segment.

    IDs reach REST paths from LLM and MCP-client input, so ``/``, ``?``,
    ``#`` and ``%`` are percent-encoded and the dot segments that HTTP
    clients normalize away are refused — an ID can never address a
    different endpoint than the one intended. A non-string ID (a number
    from a workflow event) is formatted with ``str()`` first.

    Raises:
        ConnectorError: If the ID is empty, ``.`` or ``..``.
    """
    text = str(record_id)
    if text in ("", ".", ".."):
        raise ConnectorError(f"Invalid record ID {text!r}")
    return quote(text, safe="")
