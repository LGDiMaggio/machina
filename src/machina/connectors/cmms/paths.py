"""URL-path helper shared by the CMMS REST connectors."""

from __future__ import annotations

import re
from urllib.parse import quote

from machina.exceptions import ConnectorError

# The separators a server or proxy may read in a decoded path. ``\`` counts
# because some servers treat it as ``/``.
_PATH_SEPARATORS = re.compile(r"[/\\]")


def path_segment(record_id: object) -> str:
    """Encode a caller-supplied ID as exactly one URL path segment.

    IDs reach REST paths from LLM and MCP-client input, so ``/``, ``?``,
    ``#`` and ``%`` are percent-encoded and the dot segments that HTTP
    clients normalize away are refused — an ID can never address a
    different endpoint than the one configured. An ID whose parts between
    ``/`` or ``\\`` separators include an empty, ``.`` or ``..`` part is
    refused as well: a proxy that decodes ``%2F`` before normalizing the
    path (nginx with a URI in ``proxy_pass``, which also merges ``//``)
    would resolve it into another resource or the collection itself. A
    non-string ID (a number from a workflow event or model JSON) is
    formatted with ``str()`` first.

    Raises:
        ConnectorError: If the ID is empty or has an empty, ``.`` or ``..``
            part. The message does not repeat the ID.
    """
    segment = str(record_id)
    if any(part in ("", ".", "..") for part in _PATH_SEPARATORS.split(segment)):
        raise ConnectorError(
            "Invalid record ID: an ID cannot be empty, and no part of it between "
            "'/' or '\\' separators can be empty, '.' or '..'"
        )
    return quote(segment, safe="")
