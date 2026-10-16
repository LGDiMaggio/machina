"""URL helpers shared by the REST CMMS connectors."""

from __future__ import annotations

from urllib.parse import quote

from machina.exceptions import ConnectorError


def path_segment(record_id: object) -> str:
    """Encode a caller-supplied ID as exactly one URL path segment.

    IDs reach REST paths from LLM and MCP-client input, so ``/``, ``?``,
    ``#`` and ``%`` are percent-encoded and the dot segments that HTTP
    clients normalize away are refused — an ID can never address a
    different endpoint than the one configured.

    Raises:
        ConnectorError: If the ID is empty, ``.`` or ``..``.
    """
    segment = str(record_id)
    if segment in ("", ".", ".."):
        raise ConnectorError(f"Invalid record ID {segment!r}")
    return quote(segment, safe="")
