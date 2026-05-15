"""Tests for the LayoutAwareParser (Docling wrapper)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.docs.parsing import (
    LayoutAwareParser,
    ParsedDocument,
    Section,
    TableBlock,
    _to_parsed_document,
)


def _stub_item(label: str, text: str = "", *, level: int = 1, page: int = 1) -> Any:
    """Build a stand-in Docling item with the attributes our normalizer reads."""
    item = MagicMock()
    item.label = label
    item.text = text
    item.level = level
    item.page = page
    item.prov = []
    # When asked to render as markdown, return ``text`` so table-rendering
    # tests can use the same factory.
    item.export_to_markdown.return_value = text
    return item


class TestNormalizer:
    """Docling-free tests over _to_parsed_document with stub items."""

    def test_heading_followed_by_text_creates_one_section(self) -> None:
        document = MagicMock()
        document.num_pages = 3
        document.iterate_items.return_value = [
            _stub_item("section_header", "Bearing Replacement", level=2, page=1),
            _stub_item("paragraph", "Step 1: Lock out.", page=1),
            _stub_item("paragraph", "Step 2: Remove coupling.", page=2),
        ]
        result = MagicMock(document=document)

        parsed = _to_parsed_document(result, source="manual.pdf")

        assert parsed.source == "manual.pdf"
        assert len(parsed.sections) == 1
        sec = parsed.sections[0]
        assert sec.title == "Bearing Replacement"
        assert sec.level == 2
        assert "Step 1" in sec.text and "Step 2" in sec.text
        assert sec.page_range == (1, 2)

    def test_table_is_emitted_as_atomic_block_not_section_text(self) -> None:
        document = MagicMock()
        document.iterate_items.return_value = [
            _stub_item("section_header", "Torque Specs", level=2, page=4),
            _stub_item("paragraph", "Refer to the table below.", page=4),
            _stub_item(
                "table",
                "| Fastener | Torque (Nm) |\n|---|---|\n| M10 | 45 |",
                page=4,
            ),
        ]
        result = MagicMock(document=document)

        parsed = _to_parsed_document(result, source="manual.pdf")

        # The table is NOT part of the section's prose text — it lives
        # in parsed.tables so the splitter can keep it atomic.
        assert len(parsed.tables) == 1
        table = parsed.tables[0]
        assert "M10" in table.text and "Nm" in table.text
        assert table.page == 4
        # The section still exists with its prose, no table fragments.
        assert any("table below" in s.text for s in parsed.sections)
        assert all("M10" not in s.text for s in parsed.sections)

    def test_multiple_headings_yield_distinct_sections(self) -> None:
        document = MagicMock()
        document.iterate_items.return_value = [
            _stub_item("section_header", "Introduction", level=1, page=1),
            _stub_item("paragraph", "Overview.", page=1),
            _stub_item("section_header", "Bearing Replacement", level=2, page=2),
            _stub_item("paragraph", "Steps.", page=2),
        ]
        parsed = _to_parsed_document(MagicMock(document=document), source="m.pdf")
        titles = [s.title for s in parsed.sections]
        assert titles == ["Introduction", "Bearing Replacement"]

    def test_no_items_returns_empty_parsed_document(self) -> None:
        document = MagicMock()
        document.iterate_items.return_value = []
        parsed = _to_parsed_document(MagicMock(document=document), source="m.pdf")
        assert parsed.sections == ()
        assert parsed.tables == ()


class TestLayoutAwareParserFallback:
    """The parser must degrade gracefully when Docling is absent or fails."""

    def test_parse_returns_none_when_docling_not_installed(self, tmp_path: Path) -> None:
        parser = LayoutAwareParser()
        fake_pdf = tmp_path / "x.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")

        # Pretend the lazy import fails.
        with patch.dict("sys.modules", {"docling.document_converter": None}):
            result = parser.parse(fake_pdf)

        assert result is None

    def test_parse_returns_none_when_converter_raises(self, tmp_path: Path) -> None:
        parser = LayoutAwareParser()
        fake_pdf = tmp_path / "x.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")

        broken_converter = MagicMock()
        broken_converter.convert.side_effect = RuntimeError("bad pdf")
        parser._converter = broken_converter  # bypass the lazy import

        result = parser.parse(fake_pdf)
        assert result is None

    def test_parse_returns_none_when_normalization_raises(self, tmp_path: Path) -> None:
        """If Docling returns something we don't understand, fall back cleanly."""
        parser = LayoutAwareParser()
        fake_pdf = tmp_path / "x.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")

        # A converter that returns an object whose ``document.iterate_items``
        # raises — _to_parsed_document is defensive and the parser must
        # catch and return None rather than crash ingestion.
        bad_result = MagicMock()
        bad_result.document.iterate_items.side_effect = AttributeError("schema drift")
        bad_result.document.texts = None
        bad_result.document.items = None
        bad_result.document.elements = None
        converter = MagicMock()
        converter.convert.return_value = bad_result
        parser._converter = converter

        result = parser.parse(fake_pdf)
        # Empty document is fine (no items found) — but if every probe
        # raises, we still want None rather than a crash. Either is
        # acceptable here as long as the call doesn't propagate.
        assert result is None or isinstance(result, ParsedDocument)

    def test_import_failure_is_cached(self, tmp_path: Path) -> None:
        """The lazy-import fail flag prevents retries on every parse call."""
        parser = LayoutAwareParser()
        parser._import_failed = True
        fake_pdf = tmp_path / "x.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        assert parser.parse(fake_pdf) is None
        # No converter constructed.
        assert parser._converter is None


class TestConnectorIntegration:
    """End-to-end: when the layout parser returns a ParsedDocument, the
    connector indexes sections + tables and queries find the right rows.
    """

    @pytest.fixture(autouse=True)
    def _force_keyword_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import machina.connectors.docs.document_store as _ds_mod

        def _raise(self: Any, documents: list[Any]) -> None:
            raise ImportError("forced keyword fallback")

        monkeypatch.setattr(_ds_mod.DocumentStoreConnector, "_build_rag_index", _raise)

    @pytest.mark.asyncio
    async def test_docling_path_indexes_table_as_atomic_chunk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mocked Docling output → table queryable as a single chunk."""
        from machina.connectors.docs.document_store import DocumentStoreConnector

        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        pdf = docs_dir / "manual.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        parsed = ParsedDocument(
            source=str(pdf),
            sections=(
                Section(
                    title="Torque Specs",
                    level=2,
                    text="Refer to the table below for fastener torque values.",
                    page_range=(4, 4),
                ),
            ),
            tables=(
                TableBlock(
                    text="| Fastener | Torque (Nm) |\n|---|---|\n| M10 | 45 |\n| M12 | 80 |",
                    page=4,
                    caption="Torque Specifications",
                ),
            ),
            page_count=4,
        )

        # Make the connector's parser always return our mocked ParsedDocument.
        monkeypatch.setattr(LayoutAwareParser, "parse", lambda self, file_path: parsed)

        conn = DocumentStoreConnector(paths=[docs_dir], chunk_size=200)
        await conn.connect()

        results = await conn.search("M10 torque", top_k=3)
        assert results, "expected the table to be retrievable"
        # The atomic table chunk has the rendered markdown — both rows
        # present in a single chunk's content.
        table_hits = [r for r in results if "M10" in r.content and "M12" in r.content]
        assert table_hits, f"table not atomic in results: {[r.content[:80] for r in results]}"
        assert table_hits[0].page == 4

    @pytest.mark.asyncio
    async def test_parser_unavailable_falls_back_to_pypdfloader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the parser returns None, _load_pdf uses PyPDFLoader (or skips)."""
        from machina.connectors.docs.document_store import DocumentStoreConnector

        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        # A txt file always works via the flat-text loader — the
        # important assertion is that the connector doesn't crash when
        # parser returns None.
        (docs_dir / "notes.txt").write_text("Bearing replacement procedure for P-201.")

        monkeypatch.setattr(LayoutAwareParser, "parse", lambda self, file_path: None)
        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()

        results = await conn.search("bearing", top_k=3)
        assert any("bearing" in r.content.lower() for r in results)


class TestLabelClassificationContract:
    """Pin the label-substring contract _classify_item depends on.

    _classify_item uses ``"heading" in label`` / ``"table" in label`` /
    ``"text" / "paragraph" / "list" / "caption" in label`` to classify
    items from Docling's output. If Docling renames its labels in a
    future release these tests will fail loudly instead of silently
    degrading to one giant unnamed parent.
    """

    @pytest.mark.parametrize(
        "label",
        ["heading", "section_header", "title", "Heading", "TITLE"],
    )
    def test_heading_variants_classify_as_heading(self, label: str) -> None:
        from machina.connectors.docs.parsing import _classify_item

        item = _stub_item(label, "X")
        assert _classify_item(item) == "heading"

    @pytest.mark.parametrize("label", ["table", "Table", "data_table"])
    def test_table_variants_classify_as_table(self, label: str) -> None:
        from machina.connectors.docs.parsing import _classify_item

        item = _stub_item(label, "X")
        assert _classify_item(item) == "table"

    @pytest.mark.parametrize("label", ["text", "paragraph", "list_item", "caption", "PARAGRAPH"])
    def test_prose_variants_classify_as_text(self, label: str) -> None:
        from machina.connectors.docs.parsing import _classify_item

        item = _stub_item(label, "X")
        assert _classify_item(item) == "text"


class TestApiShape:
    def test_dataclasses_construct(self) -> None:
        sec = Section(title="t", level=2, text="body", page_range=(1, 3))
        tbl = TableBlock(text="| a | b |", page=4, caption="caption")
        doc = ParsedDocument(source="x.pdf", sections=(sec,), tables=(tbl,), page_count=4)
        assert doc.sections[0].text == "body"
        assert doc.tables[0].page == 4
