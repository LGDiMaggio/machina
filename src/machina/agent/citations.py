"""Citation extraction from LLM responses.

The agent instructs the LLM to emit a structured ``<citations>`` block
at the end of every answer that uses retrieved documents. This module
parses that block, resolves each entry against the chunks actually
retrieved during the turn, and strips the block from the rendered text.

The citation contract is **index-based**: every retrieved document is
shown to the model with a visible ``[n]`` marker, and the model cites by
that index — it never has to reproduce the opaque ``chunk_id`` hash. A
source/page fallback resolves citations that name a document by filename
instead of index, so grounded answers stay attributable across models of
any strength.

The parser is intentionally tolerant: malformed blocks log a warning
but never raise.
"""

from __future__ import annotations

import re

import structlog

from machina.domain.citation import Citation

logger = structlog.get_logger(__name__)


def _safe_source(source: str) -> str:
    """Path-leak-safe form of a source string (boundary #3).

    Imported lazily from :mod:`machina.agent.prompts` to avoid a module
    import cycle: ``prompts`` imports :data:`CITATION_PROMPT` from this
    module at import time, so a top-level reverse import would deadlock.
    """
    from machina.agent.prompts import safe_source

    return safe_source(source)


CITATION_PROMPT = """\
## Document Citations

When you use information from the **Relevant Documents** section, you MUST cite \
your sources. Each document there is shown with a visible index marker like \
``[1]``, ``[2]`` — cite by that index, never by any internal id.

1. Append an inline marker ``[n]`` immediately after the sentence that uses the \
information, where ``n`` is the index of the document you relied on (e.g. \
``Replace the filter every 2000 hours [1].``).
2. At the very end of your response, emit a ``<citations>`` block listing every \
document index you relied on, one per line, in this format:

```
<citations>
[1]
[2] | optional note
</citations>
```

You may also write a line as ``{index} | {source} | {page}`` (e.g. \
``1 | pump_p201_manual.md | 12``). Use only the indices shown in the \
**Relevant Documents** section. If your answer is not grounded in any \
document, omit the block entirely.
"""


_CITATIONS_BLOCK_RE = re.compile(
    r"<citations>\s*\n(.*?)\n?</citations>\s*", re.DOTALL | re.IGNORECASE
)


def parse_response(
    text: str,
    available_chunks: dict[str, dict[str, object]],
    ordered_chunks: list[str] | None = None,
) -> tuple[str, list[Citation]]:
    """Extract citations and strip the ``<citations>`` block from ``text``.

    Args:
        text: Raw LLM output.
        available_chunks: Mapping of ``chunk_id`` → metadata dict (with
            ``source`` and ``page`` keys). Used for the source/page
            fallback when a citation names a document by filename rather
            than by its visible index.
        ordered_chunks: Ordered list of ``chunk_id`` by *display position*
            — element ``i`` is the chunk the model saw as ``[i + 1]``. An
            empty string marks a displayed-but-unregistered slot (so the
            visible index stays aligned with what the model saw even when
            a chunk had no ``chunk_id``). Defaults to an empty list.

    Returns:
        ``(rendered_text, citations)`` where ``rendered_text`` is the
        original text with the ``<citations>`` block removed but inline
        ``[n]`` / ``[source:page]`` markers preserved.
    """
    citations: list[Citation] = []
    seen_ids: set[str] = set()
    ordered = ordered_chunks or []

    def _consume(match: re.Match[str]) -> str:
        for citation in _parse_block(match.group(1), available_chunks, ordered):
            if citation.chunk_id in seen_ids:
                continue
            seen_ids.add(citation.chunk_id)
            citations.append(citation)
        return ""

    cleaned = _CITATIONS_BLOCK_RE.sub(_consume, text).rstrip()
    return cleaned, citations


# A leading visible index, optionally bracketed: ``[1]``, ``1``.
_INDEX_RE = re.compile(r"^\[?\s*(\d{1,3})\s*\]?$")


def _split_pipes(entry: str) -> list[str]:
    """Split a citation line on its first and last ``|`` only.

    The first field is the index (or source); the last is the page; the
    middle is the source. Splitting only on the outer delimiters keeps
    pipe characters inside a source path or section title (e.g.
    ``Section 5 | Maintenance``) from truncating the source field.
    """
    first = entry.find("|")
    last = entry.rfind("|")
    if first == -1:
        return [entry]
    if first == last:
        return [entry[:first].strip(), entry[first + 1 :].strip()]
    return [
        entry[:first].strip(),
        entry[first + 1 : last].strip(),
        entry[last + 1 :].strip(),
    ]


def _resolve_by_source(
    raw_source: str,
    available_chunks: dict[str, dict[str, object]],
    page: int | None,
) -> str | None:
    """Resolve a bare source (or source+page) reference to a ``chunk_id``.

    Comparison is by ``safe_source`` basename, case-insensitively, so a
    citation that names ``P-201_manual.pdf`` matches the registered
    ``manuals/p-201_manual.pdf``. Resolves only when the match is
    unambiguous; when two registered chunks share the source (and a page
    does not disambiguate) the reference is treated as ambiguous and
    dropped by the caller.
    """
    target = _safe_source(raw_source).strip().lower()
    if not target:
        return None
    matches: list[str] = []
    for chunk_id, meta in available_chunks.items():
        meta_source = _safe_source(str(meta.get("source", ""))).strip().lower()
        if meta_source != target:
            continue
        if page is not None:
            meta_page = meta.get("page", 0)
            if isinstance(meta_page, int) and meta_page != page:
                continue
        matches.append(chunk_id)
    if len(matches) == 1:
        return matches[0]
    return None


def _parse_block(
    raw_block: str,
    available_chunks: dict[str, dict[str, object]],
    ordered_chunks: list[str],
) -> list[Citation]:
    citations: list[Citation] = []
    seen_ids: set[str] = set()
    for line in raw_block.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = _split_pipes(entry)
        head = parts[0].strip()
        if not head:
            continue

        chunk_id: str | None = None
        page_field = parts[2].strip() if len(parts) > 2 else ""

        # Primary contract: a small integer indexing the turn's displayed
        # results. ``[n]`` maps to ordered_chunks[n - 1] by display
        # position, so it resolves to exactly the chunk the model saw.
        index_match = _INDEX_RE.match(head)
        if index_match is not None:
            idx = int(index_match.group(1))
            if 1 <= idx <= len(ordered_chunks):
                candidate = ordered_chunks[idx - 1]
                if candidate:
                    chunk_id = candidate

        # Fallback A: backward tolerance — a model that still echoes a raw
        # ``chunk_id`` present in the registry resolves directly. The id is
        # no longer in the prompt surface, but accepting it costs nothing.
        if chunk_id is None and index_match is None and head in available_chunks:
            chunk_id = head

        # Fallback B: the head is a bare source filename, optionally with a
        # ``source:page`` suffix. Resolve against the registry by basename.
        if chunk_id is None and index_match is None:
            fb_source = head
            fb_page: int | None = None
            colon = head.rfind(":")
            if colon != -1:
                maybe_page = head[colon + 1 :].strip()
                if maybe_page.isdigit():
                    fb_source = head[:colon].strip()
                    fb_page = int(maybe_page)
            if fb_page is None and page_field.isdigit():
                fb_page = int(page_field)
            chunk_id = _resolve_by_source(fb_source, available_chunks, fb_page)

        if chunk_id is None:
            logger.warning(
                "citation_chunk_id_not_in_context",
                operation="parse_citations",
                chunk_id=head,
            )
            continue
        if chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)

        meta = available_chunks.get(chunk_id, {})
        # The rendered source always comes from the registry (already
        # ``safe_source``-sanitised at the prompt boundary), never from
        # the model's free text — preserving the path-leak guard.
        source = _safe_source(str(meta.get("source", "")))
        page = _parse_page(page_field, meta.get("page", 0))
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
