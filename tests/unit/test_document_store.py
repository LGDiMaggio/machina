"""Tests for the DocumentStoreConnector."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any
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

    @pytest.fixture(autouse=True)
    def _force_keyword_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force the connector into keyword fallback for this test class.

        Without this fixture the test environment has ``langchain_chroma``
        and ``chromadb`` installed, so RAG mode would activate. The keyword
        tests are documenting fallback behavior, so we make ``Chroma``
        unavailable on the connector module regardless of whether
        ``langchain_chroma`` has already been imported by a previous
        test (a plain ``sys.modules`` patch wouldn't catch that case).
        """
        import machina.connectors.docs.document_store as _ds_mod

        def _build_rag_index_raises(self: Any, documents: list[Any]) -> None:
            raise ImportError("forced keyword fallback for tests")

        monkeypatch.setattr(
            _ds_mod.DocumentStoreConnector, "_build_rag_index", _build_rag_index_raises
        )

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

    @staticmethod
    async def _connect_and_capture_fallback_event(
        sample_docs_dir: Path,
    ) -> dict[str, Any]:
        """Connect in fallback mode and return the keyword_fallback log event."""
        import structlog

        events: list[dict[str, Any]] = []

        def _capture(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
            events.append(dict(event_dict))
            return event_dict

        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                _capture,
                structlog.processors.JSONRenderer(),
            ]
        )
        try:
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()
        finally:
            structlog.reset_defaults()

        fallback = [
            e
            for e in events
            if e.get("event") == "connected" and e.get("mode") == "keyword_fallback"
        ]
        assert len(fallback) == 1
        return fallback[0]

    @pytest.mark.asyncio
    async def test_keyword_fallback_warning_names_missing_package(
        self, sample_docs_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback log is a WARNING that names the missing langchain-chroma."""
        import sys

        # ``None`` in sys.modules makes find_spec return None, so the
        # legacy-install detection reports a clean (non-legacy) env even
        # though the test environment has langchain_community installed.
        monkeypatch.setitem(sys.modules, "langchain_community.vectorstores", None)

        event = await self._connect_and_capture_fallback_event(sample_docs_dir)
        assert event["level"] == "warning"
        assert event["missing_package"] == "langchain-chroma"
        assert "machina-ai[docs-rag]" in event["hint"]
        assert "legacy" not in event["hint"].lower()

    @pytest.mark.asyncio
    async def test_keyword_fallback_warning_legacy_install_remedy(
        self, sample_docs_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy install (community vectorstores present, chroma missing) →
        the WARNING names the exact pip remedy with the [docs-rag] extra."""
        import importlib.machinery
        import sys

        # Simulate the deprecated module still being importable: a bare
        # `pip install -U machina-ai` over a pre-migration [docs-rag]
        # install leaves langchain_community.vectorstores around while
        # langchain-chroma is absent. find_spec on a sys.modules entry
        # returns its __spec__, so the stub needs a real ModuleSpec.
        fake_module = MagicMock()
        fake_module.__spec__ = importlib.machinery.ModuleSpec(
            "langchain_community.vectorstores", loader=None
        )
        monkeypatch.setitem(sys.modules, "langchain_community.vectorstores", fake_module)

        event = await self._connect_and_capture_fallback_event(sample_docs_dir)
        assert event["level"] == "warning"
        assert event["missing_package"] == "langchain-chroma"
        assert 'pip install -U "machina-ai[docs-rag]"' in event["hint"]


class TestParentExpansion:
    """End-to-end tests for the Unit 5 parent-expansion contract."""

    @pytest.fixture(autouse=True)
    def _force_keyword_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import machina.connectors.docs.document_store as _ds_mod

        def _raise(self: Any, documents: list[Any]) -> None:
            raise ImportError("forced keyword fallback")

        monkeypatch.setattr(_ds_mod.DocumentStoreConnector, "_build_rag_index", _raise)

    @pytest.mark.asyncio
    async def test_query_for_one_step_returns_full_procedure(self, tmp_path: Path) -> None:
        """Plan headline: query a single step → response includes the full procedure."""
        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        (docs_dir / "P-201_pump.md").write_text(
            "# Pump P-201 Manual\n\n"
            "## Bearing Replacement Procedure\n\n"
            "Step 1: Lock out / Tag out the motor.\n"
            "Step 2: Remove coupling.\n"
            "Step 3: Use bearing puller to remove old bearings.\n"
            "Step 4: Heat new SKF 6310 bearings to 110 C.\n"
            "Step 5: Slide bearings onto shaft.\n"
        )

        conn = DocumentStoreConnector(paths=[docs_dir], chunk_size=80, chunk_overlap=0)
        await conn.connect()

        results = await conn.search("bearing puller remove old bearings", top_k=3)

        assert results, "expected at least one match"
        top = results[0]
        # All 5 steps must be in the returned content — that's the
        # whole point of parent-document retrieval.
        for step in ("Step 1", "Step 2", "Step 3", "Step 4", "Step 5"):
            assert step in top.content, f"missing {step} in expanded content"

    @pytest.mark.asyncio
    async def test_dedup_preserves_top_k_via_overfetch(self, tmp_path: Path) -> None:
        """Multiple matches sharing a parent should not collapse top_k to 1.

        With over-fetch in search() and dedup-by-parent in
        _expand_to_parents, the final list still satisfies the
        caller's top_k when the corpus has enough distinct parents.
        """
        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        (docs_dir / "doc.md").write_text(
            "# Section A\n\nbearing alpha alpha alpha.\n\n"
            "# Section B\n\nbearing beta beta beta.\n\n"
            "# Section C\n\nbearing gamma gamma gamma.\n"
        )
        conn = DocumentStoreConnector(paths=[docs_dir], chunk_size=40, chunk_overlap=0)
        await conn.connect()
        results = await conn.search("bearing", top_k=3)
        # 3 distinct sections each contain "bearing" — should get 3 results
        parent_ids = {r.parent_id for r in results if r.parent_id}
        assert len(parent_ids) == 3, f"expected 3 distinct parents, got {parent_ids}"

    @pytest.mark.asyncio
    async def test_oversized_parent_is_windowed_around_match(self, tmp_path: Path) -> None:
        """When parent exceeds max_parent_chars, _expand_to_parents windows around the match."""
        # Use a very low max_parent_chars on the splitter to force the
        # truncation path with a small fixture.
        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        big_section = "ALPHA " * 200 + "TARGET_TOKEN_XYZ " + "OMEGA " * 200
        (docs_dir / "big.md").write_text(f"# Big Section\n\n{big_section}\n")

        conn = DocumentStoreConnector(paths=[docs_dir], chunk_size=80, chunk_overlap=0)
        # Force the splitter to consider this section "oversized".
        conn._splitter.max_parent_chars = 200
        conn._splitter.parent_window = 200
        await conn.connect()

        results = await conn.search("TARGET_TOKEN_XYZ", top_k=1)
        assert results, "expected a match"
        text = results[0].content
        assert "TARGET_TOKEN_XYZ" in text, "windowing should keep the match in-frame"
        # Window must be smaller than the full parent body.
        assert len(text) < 2000, f"expected windowed content, got {len(text)} chars"

    @pytest.mark.asyncio
    async def test_orphan_parent_id_returns_raw_match(self, tmp_path: Path) -> None:
        """If parent_id is missing from the in-memory map (orphan), return the raw chunk."""
        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        (docs_dir / "doc.md").write_text("# Section\n\nbearing replacement notes.\n")
        conn = DocumentStoreConnector(paths=[docs_dir], chunk_size=60, chunk_overlap=0)
        await conn.connect()

        # Simulate a stale persistent vector by mutating chunk parent_id
        # to one not in _parent_by_id; expansion should still return a
        # result (using the raw match content).
        for c in conn._chunks:
            c.parent_id = "ORPHAN_NOT_IN_MAP"
        results = await conn.search("bearing", top_k=1)
        assert results, "orphan match should still surface"
        assert "bearing" in results[0].content.lower()


class TestSectionTitleAndTableSurfacing:
    """End-to-end: splitter-detected section_title and is_table flag reach
    the LLM-facing prompt and the MCP tool result.
    """

    @pytest.fixture(autouse=True)
    def _force_keyword_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import machina.connectors.docs.document_store as _ds_mod

        def _raise(self: Any, documents: list[Any]) -> None:
            raise ImportError("forced keyword fallback")

        monkeypatch.setattr(_ds_mod.DocumentStoreConnector, "_build_rag_index", _raise)

    @pytest.mark.asyncio
    async def test_section_title_reaches_prompt_formatter(self, tmp_path: Path) -> None:
        """search() returns chunks carrying section_title, and format_document_results
        renders it as ``§ <title>`` so the LLM sees it.
        """
        from machina.agent.prompts import format_document_results

        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        (docs_dir / "doc.md").write_text(
            "# Pump P-201 Manual\n\n"
            "## Bearing Replacement Procedure\n\n"
            "Step 1: Lock out the motor.\n"
            "Step 2: Remove coupling.\n"
        )

        conn = DocumentStoreConnector(paths=[docs_dir], chunk_size=80, chunk_overlap=0)
        await conn.connect()
        # Query for a body term ("lock out" appears in Step 1) — keyword
        # fallback ranks against match-chunk text, not the section title.
        # The parent-expanded content returned to the caller will still
        # carry the title since the splitter prefixes it.
        results = await conn.search("lock out", top_k=1)
        assert results, "expected a match"

        # Project to the agent runtime's serializer shape.
        serialized = [
            {
                "content": r.content,
                "source": r.source,
                "page": r.page,
                "chunk_id": r.chunk_id,
                "section_title": r.section_title,
                "is_table": r.is_table,
            }
            for r in results
        ]
        rendered = format_document_results(serialized)
        assert "§ Bearing Replacement Procedure" in rendered, rendered

    @pytest.mark.asyncio
    async def test_is_table_flag_surfaces_table_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a Docling-parsed table is retrieved, format_document_results tags it [TABLE]."""
        from machina.agent.prompts import format_document_results
        from machina.connectors.docs.parsing import (
            LayoutAwareParser,
            ParsedDocument,
            TableBlock,
        )

        docs_dir = tmp_path / "manuals"
        docs_dir.mkdir()
        pdf = docs_dir / "manual.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        parsed = ParsedDocument(
            source=str(pdf),
            tables=(
                TableBlock(
                    text="| Fastener | Torque (Nm) |\n|---|---|\n| M10 | 45 |",
                    page=4,
                    caption="Torque Specs",
                ),
            ),
        )
        monkeypatch.setattr(LayoutAwareParser, "parse", lambda self, file_path: parsed)

        conn = DocumentStoreConnector(paths=[docs_dir])
        await conn.connect()
        results = await conn.search("M10 torque", top_k=1)
        assert results, "expected table to be retrievable"
        assert results[0].is_table is True

        serialized = [
            {
                "content": r.content,
                "source": r.source,
                "page": r.page,
                "chunk_id": r.chunk_id,
                "section_title": r.section_title,
                "is_table": r.is_table,
            }
            for r in results
        ]
        rendered = format_document_results(serialized)
        assert "[TABLE]" in rendered, rendered


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


class TestEmbedderConfig:
    """Swappable embedder param (Unit 7)."""

    def test_no_embedder_returns_none(self) -> None:
        """Default constructor produces no custom embedding function."""
        conn = DocumentStoreConnector()
        assert conn._load_embedding_function() is None

    def test_unloadable_embedder_falls_back_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bad embedder model name → None (caller uses Chroma's default)."""
        import sys

        fake_module = MagicMock()
        fake_module.HuggingFaceEmbeddings = MagicMock(
            side_effect=RuntimeError("model download failed")
        )
        monkeypatch.setitem(sys.modules, "langchain_huggingface", fake_module)

        conn = DocumentStoreConnector(embedder="some-nonexistent-model")
        assert conn._load_embedding_function() is None

    def test_embedder_wrapper_unavailable_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing langchain_huggingface → None."""
        import sys

        monkeypatch.setitem(sys.modules, "langchain_huggingface", None)
        conn = DocumentStoreConnector(embedder="BAAI/bge-m3")
        assert conn._load_embedding_function() is None

    def test_embedder_happy_path_threads_to_chroma(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Happy path: embedder configured + model loads → embedding= flows to Chroma.from_texts."""
        import sys

        fake_embedding = MagicMock(name="HFEmbeddings instance")
        hf_ctor = MagicMock(return_value=fake_embedding)
        fake_embeddings_module = MagicMock()
        fake_embeddings_module.HuggingFaceEmbeddings = hf_ctor
        monkeypatch.setitem(sys.modules, "langchain_huggingface", fake_embeddings_module)

        conn = DocumentStoreConnector(embedder="BAAI/bge-m3")
        out = conn._load_embedding_function()

        hf_ctor.assert_called_once_with(model_name="BAAI/bge-m3")
        assert out is fake_embedding


def _isolated_docstore(*paths: Path) -> DocumentStoreConnector:
    """A connector with a per-test unique collection name.

    Unlike the other classes in this file, ``TestMetadataFiltering`` does
    not force keyword mode, so with ``chromadb`` installed it builds a
    real Chroma index. Chroma's default ephemeral client shares one
    in-process system: every connector using the default collection name
    reads (and pollutes) the same collection, leaking chunks into any
    other default-named connector in the same pytest process — this is
    what made ``test_every_chunk_carries_source_citation`` order-dependent
    flaky. Same pattern as tests/integration/test_document_store_extras.py.
    """
    return DocumentStoreConnector(
        paths=list(paths),
        collection_name=f"unit_meta_{uuid.uuid4().hex[:8]}",
    )


class TestMetadataFiltering:
    """Pre-retrieval filtering via the new metadata schema (Unit 1)."""

    @pytest.mark.asyncio
    async def test_chunks_carry_inferred_asset_id(self, sample_docs_dir: Path) -> None:
        conn = _isolated_docstore(sample_docs_dir)
        await conn.connect()
        # P-201 manual chunks should be tagged via filename inference,
        # COMP-301 manual via frontmatter.
        p201 = [c for c in conn._chunks if c.asset_id == "P-201"]
        comp = [c for c in conn._chunks if c.asset_id == "COMP-301"]
        assert len(p201) > 0
        assert len(comp) > 0

    @pytest.mark.asyncio
    async def test_asset_filter_excludes_other_assets(self, sample_docs_dir: Path) -> None:
        conn = _isolated_docstore(sample_docs_dir)
        await conn.connect()
        results = await conn.search("filter replacement", asset_id="COMP-301")
        # No P-201 results when filter is COMP-301.
        assert all(r.asset_id == "COMP-301" for r in results)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_filters_kwarg_with_doc_type(self, sample_docs_dir: Path) -> None:
        conn = _isolated_docstore(sample_docs_dir)
        await conn.connect()
        results = await conn.search("filter replacement", filters={"doc_type": "manual"})
        # Both fixtures resolve to doc_type == "manual" (one via inference,
        # one via frontmatter), so at least one result is expected.
        assert len(results) > 0
        assert all(r.doc_type == "manual" for r in results)

    @pytest.mark.asyncio
    async def test_unknown_filter_value_returns_empty(self, sample_docs_dir: Path) -> None:
        conn = _isolated_docstore(sample_docs_dir)
        await conn.connect()
        results = await conn.search("anything", filters={"asset_id": "NOT-EXISTING"})
        assert results == []

    @pytest.mark.asyncio
    async def test_chunk_id_is_deterministic(self, sample_docs_dir: Path) -> None:
        conn_a = _isolated_docstore(sample_docs_dir)
        conn_b = _isolated_docstore(sample_docs_dir)
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

        conn = _isolated_docstore(docs_dir)
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
        conn = _isolated_docstore(docs_dir)
        await conn.connect()
        sources = {c.source for c in conn._chunks}
        assert not any(s.endswith(".meta.yaml") for s in sources)


class TestDocumentStoreRAG:
    """Test DocumentStoreConnector with mocked RAG dependencies.

    These tests verify the dense-retrieval contract (Chroma mocks). The
    hybrid BM25 path is exercised separately in test_hybrid.py / via
    integration tests on real corpora — disabling BM25 here keeps the
    dense-mock assertions deterministic.
    """

    @pytest.fixture(autouse=True)
    def _disable_bm25(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force ``_build_bm25_index`` to return None so the dense path is
        the only retriever — the dense mock then fully controls results.
        """
        import machina.connectors.docs.document_store as _ds_mod

        monkeypatch.setattr(_ds_mod, "_build_bm25_index", lambda *a, **k: None)

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
                "langchain_chroma": mock_vectorstores,
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
        mock_doc.metadata = {"source": "manual.txt", "page": 1, "chunk_id": "mock-c1"}

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
                "langchain_chroma": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()

        # Now search — it should use the mocked vectorstore
        results = await conn.search("pump maintenance")
        assert len(results) == 1
        assert results[0].content == "Pump P-201 maintenance guide"
        # Hybrid mode returns an RRF-fused score, not the raw dense similarity.
        assert results[0].score > 0

    @pytest.mark.asyncio
    async def test_rag_search_with_asset_filter(self, sample_docs_dir: Path) -> None:
        """Test RAG search with asset_id filter."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        mock_doc = MagicMock()
        mock_doc.page_content = "P-201 bearing specs"
        mock_doc.metadata = {"source": "p201.txt", "page": 2, "chunk_id": "mock-c2"}

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
                "langchain_chroma": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()

        await conn.search("bearing", asset_id="P-201")
        # Verify pre-retrieval filter is passed via Chroma's ``filter=``
        # kwarg. search() over-fetches by _PARENT_OVERFETCH_FACTOR=6
        # bounded by corpus size so parent dedup can still satisfy
        # top_k after collapsing matches that share a parent. The
        # sample corpus only produces 3 chunks; the over-fetch is
        # capped at that.
        call_kwargs = mock_vectorstore.similarity_search_with_score.call_args
        assert call_kwargs[1].get("k") == len(conn._chunks)
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
                "langchain_chroma": mock_vectorstores,
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
        mock_doc.metadata = {
            "source": "p201.txt",
            "page": 1,
            "asset_id": "P-201",
            "chunk_id": "mock-p201",
        }
        mock_doc_other = MagicMock()
        mock_doc_other.page_content = "Compressor manual"
        mock_doc_other.metadata = {
            "source": "c1.txt",
            "page": 1,
            "asset_id": "COMP-301",
            "chunk_id": "mock-c301",
        }

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
                "langchain_chroma": mock_vectorstores,
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
    async def test_reranker_unavailable_preserves_rrf_order(self, sample_docs_dir: Path) -> None:
        """Reranker that returns None must NOT overwrite RRF scores."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        doc_a = MagicMock()
        doc_a.page_content = "Pump P-201 maintenance"
        doc_a.metadata = {"source": "a.txt", "page": 1, "chunk_id": "c1"}

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.return_value = [(doc_a, 0.9)]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore
        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls
        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        # Cross-encoder constructor raises → rerank returns None.
        ce_module = MagicMock()
        ce_module.CrossEncoder = MagicMock(side_effect=RuntimeError("model not cached"))

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_chroma": mock_vectorstores,
                "sentence_transformers": ce_module,
            },
        ):
            conn = DocumentStoreConnector(
                paths=[sample_docs_dir],
                reranker_model="mock-broken-reranker",
            )
            await conn.connect()
            results = await conn.search("pump")

        # Reranker failed silently → upstream RRF chunk surfaces with its
        # fused score (positive, not the zero sentinel).
        assert len(results) >= 1
        assert results[0].chunk_id == "c1"
        assert results[0].score > 0

    @pytest.mark.asyncio
    async def test_dense_only_when_bm25_extra_missing(self, sample_docs_dir: Path) -> None:
        """When the docs-rag-hybrid extra is absent, fall back to dense-only."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        doc_a = MagicMock()
        doc_a.page_content = "Pump bearing"
        doc_a.metadata = {"source": "a.txt", "page": 1, "chunk_id": "c1"}

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.return_value = [(doc_a, 0.9)]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore
        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls
        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        # Patch rank_bm25 to ImportError so BM25Index.build() degrades.
        import sys as _sys

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_chroma": mock_vectorstores,
                "rank_bm25": None,
            },
        ):
            # Drop the cached rank_bm25 import so the lazy load inside
            # BM25Index.build re-tries the stub above.
            _sys.modules.pop("rank_bm25", None)
            _sys.modules["rank_bm25"] = None  # type: ignore[assignment]
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()
            results = await conn.search("bearing")

        # Dense-only path returns the chunk; BM25 index is None.
        assert conn._bm25_index is None
        assert len(results) == 1
        assert results[0].chunk_id == "c1"

    @pytest.mark.asyncio
    async def test_reranker_overrides_rrf_order(self, sample_docs_dir: Path) -> None:
        """When a reranker is configured, its scores win over the RRF order."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        # Two candidates: dense puts c1 first; reranker says c2 wins.
        doc_a = MagicMock()
        doc_a.page_content = "Bearing replacement: lock out the motor first"
        doc_a.metadata = {
            "source": "a.txt",
            "page": 1,
            "asset_id": "P-201",
            "chunk_id": "c1",
        }
        doc_b = MagicMock()
        doc_b.page_content = "How to replace a bearing on pump P-201: full step-by-step"
        doc_b.metadata = {
            "source": "b.txt",
            "page": 1,
            "asset_id": "P-201",
            "chunk_id": "c2",
        }

        mock_vectorstore = MagicMock()
        mock_vectorstore.similarity_search_with_score.return_value = [
            (doc_a, 0.9),
            (doc_b, 0.7),
        ]
        mock_chroma_cls = MagicMock()
        mock_chroma_cls.from_texts.return_value = mock_vectorstore
        mock_text_splitter = MagicMock()
        mock_text_splitter.RecursiveCharacterTextSplitter = mock_splitter_cls
        mock_vectorstores = MagicMock()
        mock_vectorstores.Chroma = mock_chroma_cls

        # Mock CrossEncoder so c2 outscores c1.
        ce_module = MagicMock()
        ce_instance = MagicMock()
        # Scores are returned in the same order as the pairs passed in.
        # Pairs: [("c1", a-text), ("c2", b-text)] → scores [0.1, 0.95]
        ce_instance.predict.return_value = [0.1, 0.95]
        ce_module.CrossEncoder = MagicMock(return_value=ce_instance)

        with patch.dict(
            "sys.modules",
            {
                "langchain": MagicMock(),
                "langchain.text_splitter": mock_text_splitter,
                "langchain_chroma": mock_vectorstores,
                "sentence_transformers": ce_module,
            },
        ):
            conn = DocumentStoreConnector(
                paths=[sample_docs_dir],
                reranker_model="mock-reranker",
            )
            await conn.connect()
            results = await conn.search("how to replace a bearing")

        assert len(results) == 2
        # Reranker says c2 wins despite dense scoring c1 higher.
        assert results[0].chunk_id == "c2"
        assert results[1].chunk_id == "c1"
        # Final score is the reranker score, not RRF.
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_rag_search_fallback_on_valueerror(self, sample_docs_dir: Path) -> None:
        """Modern Chroma rejects malformed where with ValueError → fallback."""
        mock_splitter_cls = MagicMock()
        mock_splitter = MagicMock()
        mock_splitter.split_text.side_effect = lambda text: [text[:100]]
        mock_splitter_cls.return_value = mock_splitter

        mock_doc = MagicMock()
        mock_doc.page_content = "Pump P-201 procedure"
        mock_doc.metadata = {
            "source": "p201.txt",
            "page": 1,
            "asset_id": "P-201",
            "chunk_id": "mock-p201v",
        }

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
                "langchain_chroma": mock_vectorstores,
            },
        ):
            conn = DocumentStoreConnector(paths=[sample_docs_dir])
            await conn.connect()
            results = await conn.search("bearing", asset_id="P-201")

        assert mock_vectorstore.similarity_search_with_score.call_count == 2
        assert len(results) == 1
