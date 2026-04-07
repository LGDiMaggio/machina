"""Tests for the DocumentStoreConnector."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.docs.document_store import DocumentChunk, DocumentStoreConnector
from machina.exceptions import ConnectorError


@pytest.fixture
def sample_docs_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample documents."""
    docs_dir = tmp_path / "manuals"
    docs_dir.mkdir()

    (docs_dir / "pump_manual.txt").write_text(
        "Pump P-201 Bearing Replacement Procedure\n\n"
        "Step 1: Lock out / Tag out the motor.\n"
        "Step 2: Remove coupling.\n"
        "Step 3: Use bearing puller to remove old bearings.\n"
        "Step 4: Heat new SKF 6310 bearings to 110°C.\n"
        "Step 5: Slide bearings onto shaft.\n\n"
        "Safety: Always wear PPE including safety glasses and gloves.\n\n"
        "Vibration limits: DE < 4.5 mm/s, NDE < 3.5 mm/s"
    )

    (docs_dir / "compressor_manual.md").write_text(
        "# Air Compressor COMP-301 Manual\n\n"
        "## COMP-301 Filter Replacement\n\n"
        "Replace COMP-301 intake air filter every 2000 hours.\n"
        "Part number: FILTER-GA55-INT\n\n"
        "## Troubleshooting\n\n"
        "High temperature on COMP-301: Check cooler and oil level.\n"
        "Oil in air: Replace separator element."
    )

    return docs_dir


class TestDocumentStoreConnector:
    """Test DocumentStoreConnector in keyword fallback mode."""

    @pytest.mark.asyncio
    async def test_connect_and_load(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        health = await conn.health_check()
        assert health.status.value == "healthy"
        assert health.details["mode"] == "keyword"
        assert health.details["chunk_count"] > 0

    @pytest.mark.asyncio
    async def test_search_returns_results(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("bearing replacement")
        assert len(results) > 0
        assert any("bearing" in r.content.lower() for r in results)

    @pytest.mark.asyncio
    async def test_search_with_asset_filter(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("filter replacement", asset_id="COMP-301")
        assert len(results) > 0
        assert all("COMP-301" in r.content or "comp-301" in r.content.lower() for r in results)

    @pytest.mark.asyncio
    async def test_search_no_results(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("quantum physics")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_empty_directory(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        conn = DocumentStoreConnector(paths=[empty_dir])
        await conn.connect()
        results = await conn.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_disconnect(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        await conn.disconnect()
        health = await conn.health_check()
        assert health.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        conn = DocumentStoreConnector()
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.search("test")

    def test_capabilities(self) -> None:
        conn = DocumentStoreConnector()
        assert "search_documents" in conn.capabilities
        assert "retrieve_section" in conn.capabilities

    # --- New tests for coverage ---

    @pytest.mark.asyncio
    async def test_load_single_file(self, tmp_path: Path) -> None:
        """Pass a single file (not directory) as path."""
        txt_file = tmp_path / "single_doc.txt"
        txt_file.write_text("Single document content about valves and pumps.")
        conn = DocumentStoreConnector(paths=[txt_file])
        await conn.connect()
        results = await conn.search("valves")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_unsupported_file_extension(self, tmp_path: Path) -> None:
        """Unsupported file types (e.g. .csv) are skipped."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "data.csv").write_text("col1,col2\nval1,val2")
        (docs_dir / "manual.txt").write_text("Important maintenance procedure.")
        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()
        # Only the txt file should be loaded
        health = await conn.health_check()
        assert health.details["chunk_count"] >= 1
        results = await conn.search("maintenance")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_pdf_without_langchain(self, tmp_path: Path) -> None:
        """PDF files are skipped when langchain is not installed."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        # Create a fake PDF file (langchain won't be able to parse it anyway)
        (docs_dir / "manual.pdf").write_bytes(b"%PDF-1.4 fake content")
        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()
        # Should not crash, just skip the PDF
        health = await conn.health_check()
        assert health.details["chunk_count"] == 0

    @pytest.mark.asyncio
    async def test_docx_without_langchain(self, tmp_path: Path) -> None:
        """DOCX files are skipped when langchain is not installed."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "manual.docx").write_bytes(b"PK fake docx")
        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()
        health = await conn.health_check()
        assert health.details["chunk_count"] == 0

    @pytest.mark.asyncio
    async def test_retrieve_section_found(self, sample_docs_dir: Path) -> None:
        """retrieve_section returns content for an existing source/page."""
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        # Get a chunk to know a valid source/page
        results = await conn.search("bearing")
        assert len(results) > 0
        chunk = results[0]
        content = await conn.retrieve_section(chunk.source, chunk.page)
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_retrieve_section_not_found(self, sample_docs_dir: Path) -> None:
        """retrieve_section returns empty string for non-existent source."""
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        content = await conn.retrieve_section("nonexistent.txt", 999)
        assert content == ""

    @pytest.mark.asyncio
    async def test_retrieve_section_not_connected(self) -> None:
        """retrieve_section raises when not connected."""
        conn = DocumentStoreConnector()
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.retrieve_section("test.txt", 1)

    @pytest.mark.asyncio
    async def test_keyword_search_top_k(self, sample_docs_dir: Path) -> None:
        """Keyword search respects top_k parameter."""
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("COMP-301 pump bearing filter", top_k=2)
        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_empty_paths(self) -> None:
        """Connector with no paths connects successfully with 0 chunks."""
        conn = DocumentStoreConnector(paths=[])
        await conn.connect()
        health = await conn.health_check()
        assert health.status.value == "healthy"
        assert health.details["chunk_count"] == 0


class TestDocumentChunk:
    """Test DocumentChunk data class."""

    def test_repr(self) -> None:
        chunk = DocumentChunk("Short text", source="test.pdf", page=1)
        assert "test.pdf" in repr(chunk)

    def test_long_text_truncated_in_repr(self) -> None:
        long_text = "A" * 100
        chunk = DocumentChunk(long_text, source="test.pdf")
        assert "..." in repr(chunk)

    def test_defaults(self) -> None:
        chunk = DocumentChunk("content")
        assert chunk.source == ""
        assert chunk.page == 0
        assert chunk.score == 0.0
        assert chunk.metadata == {}
