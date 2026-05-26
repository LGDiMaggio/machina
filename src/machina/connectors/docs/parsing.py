"""Layout-aware document parser wrapping Docling.

Optional. When the ``[docs-rag-parsing]`` extra is installed the
parser produces a structured :class:`ParsedDocument` carrying
sections, tables, and per-section page ranges so the
:class:`~machina.connectors.docs.chunking.SectionAwareSplitter` can
preserve a procedure's structure and keep tables atomic.

When Docling is not installed (or fails on a specific file), the
parser returns ``None`` and callers fall back to the existing
``PyPDFLoader`` / ``Docx2txtLoader`` paths — the rest of the
retrieval pipeline keeps working with flat text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class Section:
    """A heading-bounded region of a parsed document.

    ``page_range`` is ``(start_page, end_page)`` using 1-based page
    numbers; for single-page sections both ends are equal.
    """

    title: str
    level: int
    text: str
    page_range: tuple[int, int] = (1, 1)


@dataclass(slots=True, frozen=True)
class TableBlock:
    """An atomic table block. Indexed as a single, undivided chunk."""

    text: str
    page: int = 1
    caption: str = ""


@dataclass(slots=True, frozen=True)
class ParsedDocument:
    """Structured representation of a parsed file.

    ``sections`` carry prose with heading levels; ``tables`` are kept
    separately so the splitter can refuse to break them mid-row.
    """

    source: str
    sections: tuple[Section, ...] = ()
    tables: tuple[TableBlock, ...] = ()
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class LayoutAwareParser:
    """Best-effort layout-aware parser backed by Docling.

    Usage:
        parser = LayoutAwareParser()
        parsed = parser.parse(Path("manual.pdf"))
        if parsed is None:
            # caller falls back to flat-text loader
            ...

    The Docling import is deferred until the first ``parse`` call so
    constructing the parser is free when the extra is absent. The
    parser logs and skips on per-file failures rather than raising;
    callers should treat ``None`` as "use the fallback loader".
    """

    def __init__(self) -> None:
        self._converter: Any = None
        self._import_failed = False

    def _ensure_converter(self) -> Any:
        """Lazy-import Docling and instantiate its converter once."""
        if self._converter is not None:
            return self._converter
        if self._import_failed:
            return None
        try:
            from docling.document_converter import (  # type: ignore[import-not-found,unused-ignore]
                DocumentConverter,
            )
        except ImportError:
            self._import_failed = True
            logger.info(
                "layout_parser_unavailable",
                hint="Install machina-ai[docs-rag-parsing] for layout-aware PDF parsing",
            )
            return None
        self._converter = DocumentConverter()
        return self._converter

    def parse(self, file_path: Path) -> ParsedDocument | None:
        """Parse ``file_path`` into a structured ParsedDocument.

        Returns ``None`` when Docling is not installed or the
        conversion fails. The caller falls back to the flat-text
        loader so a single bad file (or a missing extra) never blocks
        the rest of the corpus.
        """
        converter = self._ensure_converter()
        if converter is None:
            return None
        try:
            result = converter.convert(str(file_path))
        except Exception as exc:
            logger.warning(
                "layout_parser_failed",
                file=str(file_path),
                error=str(exc),
            )
            return None
        try:
            return _to_parsed_document(result, source=str(file_path))
        except Exception as exc:
            logger.warning(
                "layout_parser_normalize_failed",
                file=str(file_path),
                error=str(exc),
            )
            return None


def _to_parsed_document(result: Any, *, source: str) -> ParsedDocument:
    """Translate a Docling ``ConversionResult`` into our internal contract.

    Docling exposes a rich tree; we project it down to:
      - ``Section`` per heading-rooted block (level + accumulated body text)
      - ``TableBlock`` per table (rendered as markdown so retrieval terms match)

    Schema-tolerant: every attribute access is guarded so a Docling
    version bump that renames a field downgrades to a warning + flat
    fallback instead of crashing ingestion.
    """
    document = getattr(result, "document", None) or result
    page_count = int(getattr(document, "num_pages", 0) or 0)

    sections: list[Section] = []
    tables: list[TableBlock] = []

    # Walk the document's flattened item stream. We accumulate prose
    # under the most recent heading so each Section carries its full
    # body, and we treat tables as atomic siblings.
    current_title = ""
    current_level = 1
    current_text: list[str] = []
    current_pages: list[int] = []

    def _flush() -> None:
        if not current_text:
            return
        text = "\n".join(current_text).strip()
        if not text:
            current_text.clear()
            return
        start = min(current_pages) if current_pages else 1
        end = max(current_pages) if current_pages else start
        sections.append(
            Section(
                title=current_title,
                level=current_level,
                text=text,
                page_range=(start, end),
            )
        )
        current_text.clear()
        current_pages.clear()

    items = _iter_document_items(document)
    for item in items:
        item_type = _classify_item(item)
        page = _item_page(item)
        if item_type == "heading":
            _flush()
            current_title = _item_text(item).strip()
            current_level = int(getattr(item, "level", 1) or 1)
            if page:
                current_pages.append(page)
        elif item_type == "table":
            table_text = _render_table(item)
            if table_text:
                tables.append(TableBlock(text=table_text, page=page or 1))
        elif item_type == "text":
            text = _item_text(item)
            if text:
                current_text.append(text)
                if page:
                    current_pages.append(page)
    _flush()

    return ParsedDocument(
        source=source,
        sections=tuple(sections),
        tables=tuple(tables),
        page_count=page_count or (sections[-1].page_range[1] if sections else 0),
    )


def _iter_document_items(document: Any) -> list[Any]:
    """Extract a flat item stream from a Docling document, schema-tolerant."""
    for attr in ("iterate_items", "texts", "items", "elements"):
        candidate = getattr(document, attr, None)
        if candidate is None:
            continue
        if callable(candidate):
            try:
                return list(candidate())
            except TypeError:
                continue
        try:
            return list(candidate)
        except TypeError:
            continue
    return []


def _classify_item(item: Any) -> str:
    """Map a Docling item to one of ``heading|table|text|other``."""
    label = str(getattr(item, "label", "") or getattr(item, "type", "")).lower()
    if "heading" in label or "title" in label or "section_header" in label:
        return "heading"
    if "table" in label:
        return "table"
    if "text" in label or "paragraph" in label or "list" in label or "caption" in label:
        return "text"
    # Fallback: anything with a non-empty text attribute counts as prose.
    if _item_text(item):
        return "text"
    return "other"


def _item_text(item: Any) -> str:
    for attr in ("text", "content"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _item_page(item: Any) -> int:
    """Extract a 1-based page number from a Docling item, when present."""
    prov = getattr(item, "prov", None)
    if prov:
        try:
            first = prov[0] if not callable(prov) else next(iter(prov()))
        except (StopIteration, TypeError, IndexError):
            first = None
        if first is not None:
            page = getattr(first, "page_no", None) or getattr(first, "page", None)
            if isinstance(page, int) and page > 0:
                return page
    page = getattr(item, "page", None) or getattr(item, "page_no", None)
    if isinstance(page, int) and page > 0:
        return page
    return 0


def _render_table(item: Any) -> str:
    """Render a Docling table as Markdown so retrieval terms still match."""
    for method in ("export_to_markdown", "to_markdown", "as_markdown"):
        fn = getattr(item, method, None)
        if callable(fn):
            try:
                rendered = fn()
            except TypeError:
                continue
            if isinstance(rendered, str) and rendered.strip():
                return rendered
    text = _item_text(item)
    return text or ""
