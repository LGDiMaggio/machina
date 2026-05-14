"""Citation extraction from LLM responses.

The agent instructs the LLM to emit a structured ``<citations>`` block
at the end of every answer that uses retrieved documents. This module
parses that block, filters it against the chunks actually retrieved
during the turn, and strips it from the rendered text.

The parser is intentionally tolerant: malformed blocks log a warning
but never raise.
"""

from __future__ import annotations

import re

import structlog

from machina.domain.citation import Citation

logger = structlog.get_logger(__name__)


CITATION_PROMPT = """\
## Document Citations

When you use information from the **Relevant Documents** section, you MUST cite \
your sources:

1. Append an inline marker ``[source:page]`` immediately after the sentence that \
uses the information (e.g. ``Replace the filter every 2000 hours [manuals/comp.md:0].``).
2. At the very end of your response, emit a ``<citations>`` block listing every \
chunk you relied on, one per line, in this exact format:

```
<citations>
{chunk_id} | {source} | {page}
{chunk_id} | {source} | {page}
</citations>
```

Use only ``chunk_id`` values that appear in the **Relevant Documents** section. \
If your answer is not grounded in any document, omit the block entirely.
"""


_CITATIONS_BLOCK_RE = re.compile(
    r"<citations>\s*\n(.*?)\n?</citations>\s*", re.DOTALL | re.IGNORECASE
)


def parse_response(
    text: str, available_chunks: dict[str, dict[str, object]]
) -> tuple[str, list[Citation]]:
    """Extract citations and strip the ``<citations>`` block from ``text``.

    Args:
        text: Raw LLM output.
        available_chunks: Mapping of ``chunk_id`` → metadata dict (with
            ``source`` and ``page`` keys). Citations whose ``chunk_id``
            is not in this mapping are dropped with a warning.

    Returns:
        ``(rendered_text, citations)`` where ``rendered_text`` is the
        original text with the ``<citations>`` block removed but inline
        ``[source:page]`` markers preserved.
    """
    match = _CITATIONS_BLOCK_RE.search(text)
    if not match:
        return text.rstrip(), []

    raw_block = match.group(1)
    cleaned = (text[: match.start()] + text[match.end() :]).rstrip()
    citations = _parse_block(raw_block, available_chunks)
    return cleaned, citations


def _parse_block(raw_block: str, available_chunks: dict[str, dict[str, object]]) -> list[Citation]:
    citations: list[Citation] = []
    seen_ids: set[str] = set()
    for line in raw_block.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = [p.strip() for p in entry.split("|")]
        if not parts or not parts[0]:
            continue
        chunk_id = parts[0]
        if chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)

        if chunk_id not in available_chunks:
            logger.warning(
                "citation_chunk_id_not_in_context",
                component="agent.citations",
                chunk_id=chunk_id,
            )
            continue

        meta = available_chunks[chunk_id]
        source = str(parts[1]) if len(parts) > 1 and parts[1] else str(meta.get("source", ""))
        page = _parse_page(parts[2] if len(parts) > 2 else "", meta.get("page", 0))
        citations.append(Citation(chunk_id=chunk_id, source=source, page=page))
    return citations


def _parse_page(raw: str, fallback: object) -> int:
    raw = raw.strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    if isinstance(fallback, int):
        return max(0, fallback)
    if isinstance(fallback, str):
        try:
            return max(0, int(fallback))
        except ValueError:
            return 0
    return 0
