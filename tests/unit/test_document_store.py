"""Tests for the DocumentStoreConnector."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

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

    (docs_dir / "P-201_pump_manual.txt").write_text(
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
        "---\n"
        "asset_id: COMP-301\n"
        "doc_type: manual\n"
        "equipment_class_code: CO\n"
        "---\n"
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
        assert chunk.chunk_id == ""
        assert chunk.asset_id == ""
        assert chunk.doc_type == ""

    def test_optional_metadata_fields(self) -> None:
        chunk = DocumentChunk(
            "content",
            chunk_id="abc123",
            asset_id="P-201",
            doc_type="manual",
            section_title="Bearing Replacement",
        )
        assert chunk.chunk_id == "abc123"
        assert chunk.asset_id == "P-201"


class TestMetadataFiltering:
    """Pre-retrieval filtering via the new metadata schema (Unit 1)."""

    @pytest.mark.asyncio
    async def test_chunks_carry_inferred_asset_id(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        # P-201 manual chunks should be tagged via filename inference,
        # COMP-301 manual via frontmatter.
        p201 = [c for c in conn._chunks if c.asset_id == "P-201"]
        comp = [c for c in conn._chunks if c.asset_id == "COMP-301"]
        assert len(p201) > 0
        assert len(comp) > 0

    @pytest.mark.asyncio
    async def test_asset_filter_excludes_other_assets(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("filter replacement", asset_id="COMP-301")
        # No P-201 results when filter is COMP-301.
        assert all(r.asset_id == "COMP-301" for r in results)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_filters_kwarg_with_doc_type(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("filter replacement", filters={"doc_type": "manual"})
        # Both fixtures resolve to doc_type == "manual" (one via inference,
        # one via frontmatter), so at least one result is expected.
        assert len(results) > 0
        assert all(r.doc_type == "manual" for r in results)

    @pytest.mark.asyncio
    async def test_unknown_filter_value_returns_empty(self, sample_docs_dir: Path) -> None:
        conn = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn.connect()
        results = await conn.search("anything", filters={"asset_id": "NOT-EXISTING"})
        assert results == []

    @pytest.mark.asyncio
    async def test_chunk_id_is_deterministic(self, sample_docs_dir: Path) -> None:
        conn_a = DocumentStoreConnector(paths=[sample_docs_dir])
        conn_b = DocumentStoreConnector(paths=[sample_docs_dir])
        await conn_a.connect()
        await conn_b.connect()
        ids_a = sorted(c.chunk_id for c in conn_a._chunks)
        ids_b = sorted(c.chunk_id for c in conn_b._chunks)
        assert ids_a == ids_b
        # All ids non-empty
        assert all(cid for cid in ids_a)

    @pytest.mark.asyncio
    async def test_sidecar_yaml_overrides_inference(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        # Filename suggests P-105, sidecar says P-999.
        pdf = docs_dir / "P-105_notes.txt"
        pdf.write_text("Inspection notes for the unit.\n", encoding="utf-8")
        sidecar = docs_dir / "P-105_notes.txt.meta.yaml"
        sidecar.write_text("asset_id: P-999\ndoc_type: procedure\n", encoding="utf-8")

        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()
        assert all(c.asset_id == "P-999" for c in conn._chunks)
        assert all(c.doc_type == "procedure" for c in conn._chunks)

    @pytest.mark.asyncio
    async def test_sidecar_files_not_indexed_as_content(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("# Guide body\n", encoding="utf-8")
        # Sidecar should be loaded as metadata, not indexed as a document.
        (docs_dir / "guide.md.meta.yaml").write_text("asset_id: P-1\n", encoding="utf-8")
        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()
        sources = {c.source for c in conn._chunks}
        assert not any(s.endswith(".meta.yaml") for s in sources)


class TestDocumentStoreRAG:
    """Test DocumentStoreConnector with mocked RAG dependencies."""

    @pytest.mark.asyncio
    async def test_rag_mode_connect(self, sample_docs_dir: Path) -> None:
        """When langchain is available, connector uses RAG mode."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        mock_chroma_cls = MagicMock()
        mock_vectorstore = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore

        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls

        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_community": MagicMock(),
                "langchain_community.vectorstores": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()

        health = await conn.health_check()
        assert health.details["mode"] == "rag"
        assert health.details["chunk_count"] > 0

    @pytest.mark.asyncio
    async def test_rag_search(self, sample_docs_dir: Path) -> None:
        """Test search in RAG mode with mocked vector store."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        # Mock search results
        mock_doc = MagicMock()
        mock_doc.page_content = "Pump P-201 maintenance guide"
        mock_doc.metadata = {"source": "manual.txt", "page": 1}

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.return_value = [(mock_doc, 0.85)]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore

        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls

        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_community": MagicMock(),
                "langchain_community.vectorstores": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()

        # Now search — it should use the mocked vectorstore
        results = await conn.search("pump maintenance")
        assert len(results) == 1
        assert results[0].content == "Pump P-201 maintenance guide"
        assert results[0].score == 0.85

    @pytest.mark.asyncio
    async def test_rag_search_with_asset_filter(self, sample_docs_dir: Path) -> None:
        """Test RAG search with asset_id filter."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        mock_doc = MagicMock()
        mock_doc.page_content = "P-201 bearing specs"
        mock_doc.metadata = {"source": "p201.txt", "page": 2}

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.return_value = [(mock_doc, 0.9)]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore

        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls

        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_community": MagicMock(),
                "langchain_community.vectorstores": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()

        await conn.search("bearing", asset_id="P-201")
        # Verify pre-retrieval filter is passed via Chroma's ``filter=`` kwarg
        # (replaces the previous post-filter substring hack).
        call_kwargs = mock_vectorstore.similarity_search_with_score.call_args
        assert call_kwargs[1].get("k") == 5
        assert call_kwargs[1].get("filter") == {"asset_id": "P-201"}

    @pytest.mark.asyncio
    async def test_rag_search_empty_vectorstore(self, tmp_path: Path) -> None:
        """RAG search with empty vectorstore returns empty list."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.return_value = []
        mock_splitter_cls.return_value = mock_splitter

        mock_chroma_cls = MagicMock()
        # from_texts is never called because texts is empty

        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls

        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_community": MagicMock(),
                "langchain_community.vectorstores": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[empty_dir])
            await conn.connect()

        results = await conn.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_rag_search_fallback_on_typeerror(self, sample_docs_dir: Path) -> None:
        """Older Chroma without ``filter=`` raises TypeError → post-filter fallback."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        mock_doc = MagicMock()
        mock_doc.page_content = "Pump P-201 procedure"
        mock_doc.metadata = {"source": "p201.txt", "page": 1, "asset_id": "P-201"}
        mock_doc_other = MagicMock()
        mock_doc_other.page_content = "Compressor manual"
        mock_doc_other.metadata = {"source": "c1.txt", "page": 1, "asset_id": "COMP-301"}

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.side_effect = [
            TypeError("filter is not a recognized argument"),
            [(mock_doc, 0.9), (mock_doc_other, 0.8)],
        ]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore

        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls
        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_community": MagicMock(),
                "langchain_community.vectorstores": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()
            results = await conn.search("bearing", asset_id="P-201")

        # First call (with filter=) raised TypeError → fallback used; the
        # post-filter dropped the COMP-301 chunk.
        assert mock_vectorstore.similarity_search_with_score.call_count == 2
        assert len(results) == 1
        assert "P-201" in results[0].content

    @pytest.mark.asyncio
    async def test_rag_search_fallback_on_valueerror(self, sample_docs_dir: Path) -> None:
        """Modern Chroma rejects malformed where with ValueError → fallback."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        mock_doc = MagicMock()
        mock_doc.page_content = "Pump P-201 procedure"
        mock_doc.metadata = {"source": "p201.txt", "page": 1, "asset_id": "P-201"}

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.side_effect = [
            ValueError("Expected where to have exactly one operator"),
            [(mock_doc, 0.9)],
        ]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore

        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls
        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_community": MagicMock(),
                "langchain_community.vectorstores": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()
            results = await conn.search("bearing", asset_id="P-201")

        assert mock_vectorstore.similarity_search_with_score.call_count == 2
        assert len(results) == 1
