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

# A ``<citations>`` opener with no closing tag. Weak models sometimes open the
# block but never close it; without this the literal ``<citations>`` tag leaks
# into the rendered answer. Applied only AFTER the closed-block pattern above,
# so it can match a genuinely dangling opener — everything from the tag to the
# end of the text is consumed and any entries it contains are still parsed.
_UNTERMINATED_CITATIONS_RE = re.compile(r"<citations>[^\S\n]*\n?(.*)\Z", re.DOTALL | re.IGNORECASE)


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

    def _absorb(block: str) -> None:
        for citation in _parse_block(block, available_chunks, ordered):
            if citation.chunk_id in seen_ids:
                continue
            seen_ids.add(citation.chunk_id)
            citations.append(citation)

    def _consume(match: re.Match[str]) -> str:
        _absorb(match.group(1))
        return ""

    cleaned = _CITATIONS_BLOCK_RE.sub(_consume, text)

    # Tolerate an unterminated block (opener with no closing tag) so the literal
    # ``<citations>`` tag never reaches the user. Runs after the closed-block
    # substitution above, so it only fires on a genuinely dangling opener.
    unterminated = _UNTERMINATED_CITATIONS_RE.search(cleaned)
    if unterminated is not None:
        _absorb(unterminated.group(1))
        cleaned = cleaned[: unterminated.start()]

    return cleaned.rstrip(), citations


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


# An inline citation marker as it appears in prose: a strictly-bracketed
# 1-3 digit index. Deliberately narrow — ``[source:page]`` forms, bracketed
# words, and 4+-digit numbers never match (strip policy, U3).
_INLINE_MARKER_RE = re.compile(r"\[(\d{1,3})\]")

# Code regions where a bracketed digit is literal text, never a citation
# marker: fenced ``` blocks (closing fence optional — truncation-tolerant)
# and inline backtick spans.
_FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Spans of ``text`` where ``[n]`` is literal, not a citation marker."""
    spans: list[tuple[int, int]] = [m.span() for m in _FENCED_CODE_RE.finditer(text)]
    for m in _INLINE_CODE_RE.finditer(text):
        start, end = m.span()
        if not any(a <= start and end <= b for a, b in spans):
            spans.append((start, end))
    return spans


def _iter_inline_markers(text: str) -> list[re.Match[str]]:
    """Inline ``[n]`` marker matches, skipping code spans and markdown links.

    A match immediately followed by ``(`` is markdown-link syntax
    (``[1](https://...)``) and is never treated as a citation marker.
    """
    protected = _protected_spans(text)
    markers: list[re.Match[str]] = []
    for m in _INLINE_MARKER_RE.finditer(text):
        if any(a <= m.start() < b for a, b in protected):
            continue
        if m.end() < len(text) and text[m.end()] == "(":
            continue
        markers.append(m)
    return markers


def renormalize_markers(
    text: str,
    citations: list[Citation],
    ordered_chunks: list[str],
) -> tuple[str, list[Citation]]:
    """Renormalize inline ``[n]`` markers to a clean ``1..N`` at the egress.

    The model cites by per-turn display index, which can be arbitrary
    (``[6]``, ``[12]``) and means nothing to the user. This pass — run once,
    at the sole egress, AFTER the validator chain — rewrites the surviving
    prose so users always see ``[1][2]...`` and reorders ``citations`` to
    match, establishing the invariant *citations list order == displayed
    number order* (the channel footer enumerates the list 1-based).

    Numbering and mapping rules:

    * The mapping is marker → ``chunk_id`` → number, never positional: a raw
      marker ``[n]`` resolves via ``ordered_chunks[n - 1]`` to a ``chunk_id``,
      and two raw indices resolving to the same deduped chunk share one
      number.
    * Numbers are assigned by order of FIRST APPEARANCE in the prose;
      citations whose chunk has no inline marker (block-only) are appended
      AFTER, in their parsed order.
    * Fail-closed: a marker is resolvable ONLY when its chunk is present in
      the parsed ``citations`` list. In-range markers with no block entry are
      stripped exactly like out-of-range ones — a :class:`Citation` is never
      synthesized for a stray marker.
    * Markers inside code spans (fenced blocks, inline backticks) or
      markdown-link syntax (``[1](...)``) are literal text and left alone.
    * With zero parsed citations the text is returned byte-identical — no
      renumbering, no stripping.

    Args:
        text: The final rendered prose (citations block already stripped,
            validator chain already run).
        citations: Citations parsed for this turn (deduped by ``chunk_id``).
        ordered_chunks: ``chunk_id`` by display position — the same map
            :func:`parse_response` resolves block entries against.

    Returns:
        ``(rewritten_text, reordered_citations)``.
    """
    if not citations:
        return text, list(citations)
    cited: dict[str, Citation] = {c.chunk_id: c for c in citations}
    assigned: dict[str, int] = {}
    pieces: list[str] = []
    pos = 0
    for m in _iter_inline_markers(text):
        raw_index = int(m.group(1))
        chunk_id = ""
        if 1 <= raw_index <= len(ordered_chunks):
            chunk_id = ordered_chunks[raw_index - 1]
        if chunk_id and chunk_id in cited:
            number = assigned.setdefault(chunk_id, len(assigned) + 1)
            pieces.append(text[pos : m.start()])
            pieces.append(f"[{number}]")
        else:
            # Unresolvable (out-of-range, empty display slot, or no block
            # entry): strip the marker plus any immediately preceding inline
            # whitespace, so "hours [9]." collapses to "hours.".
            start = m.start()
            while start > pos and text[start - 1] in " \t":
                start -= 1
            pieces.append(text[pos:start])
        pos = m.end()
    pieces.append(text[pos:])
    reordered: list[Citation] = [cited[chunk_id] for chunk_id in assigned]
    reordered.extend(c for c in citations if c.chunk_id not in assigned)
    return "".join(pieces), reordered


def strip_markers(text: str) -> str:
    """Remove every inline ``[n]`` citation marker from ``text``.

    Used for the assistant text stored in conversation history (fail-closed):
    a renormalized ``[1]`` kept in history would always be in-range against
    the NEXT turn's fresh registry, so an echoed marker would silently
    resolve to a different chunk. Code spans and markdown-link syntax are
    preserved (same skip rules as :func:`renormalize_markers`); any other
    text — including the trailing ``[Sources used in this answer: ...]``
    note, which never matches the 1-3 digit pattern — is untouched.

    Args:
        text: Text possibly carrying inline ``[n]`` markers.

    Returns:
        ``text`` with all markers (and their immediately preceding inline
        whitespace) removed.
    """
    pieces: list[str] = []
    pos = 0
    for m in _iter_inline_markers(text):
        start = m.start()
        while start > pos and text[start - 1] in " \t":
            start -= 1
        pieces.append(text[pos:start])
        pos = m.end()
    pieces.append(text[pos:])
    return "".join(pieces)


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
