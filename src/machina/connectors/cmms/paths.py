"""URL-path helper shared by the CMMS REST connectors."""

from __future__ import annotations

import re
from urllib.parse import quote

from machina.exceptions import ConnectorError

# The separators a server or proxy may read in a decoded path. ``\`` counts
# because some servers treat it as ``/``.
_PATH_SEPARATORS = re.compile(r"[/\\]")


def checked_path_id(record_id: object) -> str:
    """Return a caller-supplied ID as text, refusing one that could leave its path segment.

    IDs reach REST paths from LLM and MCP-client input. Percent-encoding
    keeps ``/`` inside one segment for the HTTP client, but a proxy that
    decodes ``%2F`` before normalizing the path (nginx with a URI in
    ``proxy_pass``, which also merges ``//``) would resolve an empty, ``.``
    or ``..`` part into another resource or the collection itself. An ID
    with such a part between ``/`` or ``\\`` separators is therefore
    refused; ``""``, ``.`` and ``..`` are the one-part cases. A part is
    judged by what precedes its first ``;``, because a servlet container
    drops ``;`` path parameters before resolving dot segments (``..;`` is
    ``..`` there). A non-string ID (a number from a workflow event or model
    JSON) is formatted with ``str()``.

    Raises:
        ConnectorError: If the ID is empty or has an empty, ``.`` or ``..``
            part. The message does not repeat the ID.
    """
    text = str(record_id)
    parts = _PATH_SEPARATORS.split(text)
    if any(part.split(";", 1)[0] in ("", ".", "..") for part in parts):
        raise ConnectorError(
            "Invalid record ID: an ID cannot be empty, and no part of it between "
            "'/' or '\\' separators can be empty, '.' or '..', including before a ';'"
        )
    return text


def path_segment(record_id: object) -> str:
    """Encode a caller-supplied ID as exactly one URL path segment.

    ``/``, ``?``, ``#`` and ``%`` are percent-encoded, and an ID that could
    still leave the segment is refused (:func:`checked_path_id`), so an ID
    can never address a different endpoint than the one configured.

    Raises:
        ConnectorError: If the ID is empty or has an empty, ``.`` or ``..``
            part. The message does not repeat the ID.
    """
    return quote(checked_path_id(record_id), safe="")
