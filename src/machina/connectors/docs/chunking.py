"""Section-aware splitter for the DocumentStoreConnector.

Splits a document into two parallel structures:

* ``ParentSection`` — one per detected section. The full section text is
  returned to the LLM at answer time so a multi-step procedure stays
  together.
* ``MatchChunk`` — small passages used for embedding / BM25 / rerank.
  Each match carries the ``parent_id`` of the section it came from so
  callers can expand to the parent after ranking.

Heading detection:

* Markdown: lines matching ``^#{1,6}\\s+TITLE``.
* Flat text (PDF extraction): best-effort regex on ``^\\d+(\\.\\d+)*\\s+[A-Z...]``
  numbered headings and ALL-CAPS heading lines.

When no headings are detected the splitter falls back to a recursive
character split and emits one parent per match so downstream parent
expansion is a no-op.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import pairwise


@dataclass(slots=True, frozen=True)
class ParentSection:
    """A full section of a document, returned to the LLM after retrieval.

    ``title_offset`` is the position in ``text`` where the section body
    begins (after any title line + blank separator the splitter prepends).
    Windowing logic adds this offset to a match's ``start_offset`` to
    locate the match inside the parent.
    """

    parent_id: str
    title: str
    level: int
    text: str
    title_offset: int = 0


@dataclass(slots=True)
class MatchChunk:
    """A small passage used for embedding / BM25 / rerank.

    ``parent_id`` joins back to a :class:`ParentSection` for expansion.
    ``start_offset`` is the character offset of this match inside the
    parent section's ``text`` so windowing for oversized parents doesn't
    have to rely on a fragile substring search.
    """

    text: str
    parent_id: str
    section_title: str
    section_level: int
    index_in_section: int
    source: str = ""
    start_offset: int = 0


# Markdown ATX heading: 1-6 '#' followed by space + title.
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# Numbered heading like "1. INTRO", "2.1 Bearing Replacement".
# Requires a capitalised first word so we don't catch "1. step one".
_NUM_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][^\n]{1,120})$")

# Word-level ALL-CAPS detection — used to flag headings like
# "BEARING REPLACEMENT PROCEDURE". We require >=2 words to avoid catching
# single-word shouts inside paragraphs.
_CAPS_WORD_RE = re.compile(r"^[A-Z][A-Z0-9\-/&]*$")


def _is_all_caps_heading(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 3 or len(stripped) > 120:
        return False
    words = stripped.split()
    if len(words) < 2:
        return False
    return all(_CAPS_WORD_RE.match(w) for w in words)


@dataclass
class _Heading:
    line_idx: int
    level: int
    title: str


_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def _has_blank_neighbour(lines: list[str], idx: int) -> bool:
    """True when a heading candidate is set off by blank lines.

    Used to suppress false positives from numbered list items and
    ALL-CAPS warning callouts inside body prose. We accept either a
    blank line *before* (start-of-document counts) or a blank line
    *after* — strict both-sides would reject legitimate trailing
    headings.
    """
    before_blank = idx == 0 or not lines[idx - 1].strip()
    after_blank = idx == len(lines) - 1 or not lines[idx + 1].strip()
    return before_blank or after_blank


def _detect_headings(lines: list[str]) -> list[_Heading]:
    """Find headings in order. Empty list ⇒ caller falls back to recursive split.

    Skips lines inside fenced code blocks (``` or ~~~) so hash-comment
    code samples don't produce phantom Markdown headings. Numbered and
    ALL-CAPS heuristics additionally require a blank-line neighbour so
    body lines like "1. Lock out the motor." or warning callouts like
    "DO NOT OPERATE" inside prose aren't promoted to section headings.
    """
    out: list[_Heading] = []
    in_fence = False
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            continue
        md = _MD_HEADING_RE.match(line)
        if md:
            level = len(md.group(1))
            out.append(_Heading(line_idx=i, level=level, title=md.group(2).strip()))
            continue
        # Flat-text heuristics — require blank-line context to avoid
        # mistaking body lines or list items for section headings.
        if not _has_blank_neighbour(lines, i):
            continue
        num = _NUM_HEADING_RE.match(line)
        if num:
            # Depth = number of dotted components. Title is the text
            # *after* the numbering so downstream filters can match
            # on a stable section name regardless of renumbering.
            depth = num.group(1).count(".") + 1
            out.append(_Heading(line_idx=i, level=depth, title=num.group(2).strip()))
            continue
        if _is_all_caps_heading(line):
            out.append(_Heading(line_idx=i, level=1, title=line.strip()))
    return out


def _section_id(source: str, title: str, line_idx: int) -> str:
    key = "\x00".join((source, title, str(line_idx)))
    return hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest()


def _recursive_split(text: str, *, chunk_size: int, chunk_overlap: int) -> list[tuple[str, int]]:
    """Best-effort character splitter with paragraph/line/word fallbacks.

    Returns ``[(piece, start_offset)]`` so callers can locate each piece
    inside the original ``text`` without a substring search. Mirrors
    LangChain's :class:`RecursiveCharacterTextSplitter` semantics closely
    enough that we don't add a runtime dependency on LangChain just for
    fallback splitting.

    Overlap is bounded so no produced chunk exceeds ``chunk_size`` —
    that's a soft contract LangChain itself sometimes violates but we
    keep tight here so downstream token budgets stay honest.
    """
    if not text.strip():
        return []
    if len(text) <= chunk_size:
        return [(text, 0)]

    separators = ["\n\n", "\n", " ", ""]
    out: list[tuple[str, int]] = [(text, 0)]
    for sep in separators:
        # Re-split any chunk that's still over the budget with the next,
        # finer separator. We never re-split chunks already within
        # budget — that would change earlier offsets.
        if sep and sep not in text:
            continue
        next_out: list[tuple[str, int]] = []
        for chunk_text, chunk_start in out:
            if len(chunk_text) <= chunk_size:
                next_out.append((chunk_text, chunk_start))
                continue
            pieces = chunk_text.split(sep) if sep else list(chunk_text)
            buf = ""
            buf_offset = 0
            cursor = chunk_start
            for piece in pieces:
                candidate = buf + (sep if buf and sep else "") + piece if sep else buf + piece
                if len(candidate) <= chunk_size:
                    if not buf:
                        buf_offset = cursor
                    buf = candidate
                    cursor += len(piece) + (len(sep) if sep else 0)
                    continue
                if buf:
                    next_out.append((buf, buf_offset))
                buf = piece
                buf_offset = cursor
                cursor += len(piece) + (len(sep) if sep else 0)
            if buf:
                next_out.append((buf, buf_offset))
        out = next_out
        if all(len(c) <= chunk_size for c, _ in out):
            break

    if chunk_overlap and len(out) > 1:
        # Prepend tail of previous chunk to each subsequent piece while
        # keeping the result within chunk_size. The match-chunk start
        # offset shifts back by the appended tail length.
        overlapped: list[tuple[str, int]] = [out[0]]
        for (prev_text, _), (cur_text, cur_offset) in pairwise(out):
            tail_len = min(chunk_overlap, len(prev_text), chunk_size - len(cur_text))
            if tail_len <= 0:
                overlapped.append((cur_text, cur_offset))
                continue
            tail = prev_text[-tail_len:]
            if cur_text.startswith(tail):
                overlapped.append((cur_text, cur_offset))
            else:
                overlapped.append((tail + cur_text, cur_offset - tail_len))
        out = overlapped
    return [(c, o) for c, o in out if c.strip()]


class SectionAwareSplitter:
    """Split text into ``(parents, matches)`` keyed by detected sections.

    Args:
        chunk_size: Target match-chunk size in characters.
        chunk_overlap: Overlap between consecutive matches in characters.
        max_parent_chars: If a detected section exceeds this size, the
            parent text is truncated around each match (with a window of
            ``parent_window`` chars on either side) and a warning is
            logged by the caller.
        parent_window: Surrounding-window size when truncating oversized
            parents.
    """

    def __init__(
        self,
        *,
        chunk_size: int = 1200,
        chunk_overlap: int = 150,
        max_parent_chars: int = 8000,
        parent_window: int = 2000,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_parent_chars = max_parent_chars
        self.parent_window = parent_window

    def split(
        self,
        text: str,
        *,
        source: str = "",
    ) -> tuple[list[ParentSection], list[MatchChunk]]:
        """Split ``text`` into parent sections and small match chunks.

        Args:
            text: The document body. Markdown is detected when ``#``
                headings are present; otherwise flat-text heuristics
                (numbered headings + ALL-CAPS, both requiring blank-line
                context) try to find sections. When no headings are
                detected the splitter falls back to a recursive
                character split with one parent per match.
            source: Optional source identifier (file path or URL) used
                to derive deterministic ``parent_id`` hashes so a
                re-ingest from the same source can be diffed.

        Returns:
            ``(parents, matches)``. Every ``MatchChunk.parent_id`` joins
            back to exactly one ``ParentSection.parent_id``, and the
            match's ``start_offset`` plus the parent's ``title_offset``
            locates the match inside ``parent.text``.
        """
        if not text.strip():
            return [], []

        lines = text.splitlines()
        headings = _detect_headings(lines)
        if not headings:
            return self._fallback_split(text, source=source)

        # Build section spans: [(heading, start_line, end_line_exclusive)].
        spans: list[tuple[_Heading, int, int]] = []
        # If there's content before the first heading, treat it as a
        # "preamble" section with title "" and level 0.
        if headings[0].line_idx > 0:
            spans.append((_Heading(line_idx=0, level=0, title=""), 0, headings[0].line_idx))
        for idx, h in enumerate(headings):
            start = h.line_idx + 1
            end = headings[idx + 1].line_idx if idx + 1 < len(headings) else len(lines)
            spans.append((h, start, end))

        parents: list[ParentSection] = []
        matches: list[MatchChunk] = []
        for heading, start, end in spans:
            section_text = "\n".join(lines[start:end]).strip()
            if not section_text:
                continue
            parent_id = _section_id(source, heading.title, heading.line_idx)
            title_offset = 0
            full_text = section_text
            if heading.title:
                # Include the title in the parent body so the LLM sees the
                # context label that the chunk was nested under. The body
                # starts at title_offset inside parent.text.
                prefix = f"{heading.title}\n\n"
                title_offset = len(prefix)
                full_text = prefix + section_text
            parents.append(
                ParentSection(
                    parent_id=parent_id,
                    title=heading.title,
                    level=heading.level,
                    text=full_text,
                    title_offset=title_offset,
                )
            )
            pieces = _recursive_split(
                section_text,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )
            if not pieces:
                pieces = [(section_text, 0)]
            for i, (piece_text, piece_offset) in enumerate(pieces):
                matches.append(
                    MatchChunk(
                        text=piece_text,
                        parent_id=parent_id,
                        section_title=heading.title,
                        section_level=heading.level,
                        index_in_section=i,
                        source=source,
                        start_offset=piece_offset,
                    )
                )
        return parents, matches

    def window_parent(self, parent: ParentSection, match: MatchChunk) -> str:
        """Return parent.text or a windowed view when it exceeds the budget.

        When the parent is within ``max_parent_chars``, return its text
        unchanged. Otherwise carve a ``parent_window``-sized slice
        centred on the match's known character offset inside the parent
        body (no fragile substring search).
        """
        text = parent.text
        if len(text) <= self.max_parent_chars:
            return text
        match_pos = parent.title_offset + match.start_offset
        half = self.parent_window // 2
        start = max(0, match_pos - half)
        end = min(len(text), match_pos + len(match.text) + half)
        return text[start:end]

    def _fallback_split(
        self, text: str, *, source: str
    ) -> tuple[list[ParentSection], list[MatchChunk]]:
        """No headings detected — each match is its own parent."""
        pieces = _recursive_split(
            text, chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
        )
        parents: list[ParentSection] = []
        matches: list[MatchChunk] = []
        for i, (piece_text, _piece_offset) in enumerate(pieces):
            parent_id = _section_id(source, "", i)
            parents.append(ParentSection(parent_id=parent_id, title="", level=0, text=piece_text))
            matches.append(
                MatchChunk(
                    text=piece_text,
                    parent_id=parent_id,
                    section_title="",
                    section_level=0,
                    index_in_section=i,
                    source=source,
                    start_offset=0,
                )
            )
        return parents, matches
