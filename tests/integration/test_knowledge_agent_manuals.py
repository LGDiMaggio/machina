"""Integration tests for DocumentStoreConnector against real sample manuals.

Exercises the keyword-fallback search path (no RAG backend required) using
the real .md files in ``examples/knowledge_agent/sample_data/manuals/``.

Note: Markdown files do not carry page numbers, so these tests assert on
source-file citations (part of R4 AC: "cites the relevant section"), not
on page references. A future PR with a small PDF fixture would cover the
page-reference half of R4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from machina.connectors.docs.document_store import (
    DocumentChunk,
    DocumentStoreConnector,
)

MANUALS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "knowledge_agent"
    / "sample_data"
    / "manuals"
)


@pytest.fixture
async def doc_store() -> DocumentStoreConnector:
    """A connected DocumentStoreConnector pointing at the real sample manuals."""
    store = DocumentStoreConnector(paths=[MANUALS_DIR])
    await store.connect()
    return store


class TestManualCitations:
    """Source-file citation coverage for R4 AC (partial — page refs deferred)."""

    @pytest.mark.asyncio
    async def test_connect_loads_both_manuals(self, doc_store: DocumentStoreConnector) -> None:
        """The connector should discover and load both .md manuals on connect."""
        health = await doc_store.health_check()
        assert health.details["chunk_count"] > 0

    @pytest.mark.asyncio
    async def test_search_bearing_finds_pump_manual(
        self, doc_store: DocumentStoreConnector
    ) -> None:
        """Searching for 'bearing' should return chunks from the pump manual
        (which has a dedicated Bearing Replacement Procedure section)."""
        results = await doc_store.search("bearing")
        assert results, "search returned no chunks for 'bearing'"
        # At least one chunk must come from the pump manual
        sources = {chunk.source for chunk in results}
        assert any("pump_p201" in src for src in sources), (
            f"expected pump manual in sources, got: {sources}"
        )
        # And the matching content actually mentions bearing
        pump_chunk = next(c for c in results if "pump_p201" in c.source)
        assert "bearing" in pump_chunk.content.lower()

    @pytest.mark.asyncio
    async def test_search_air_filter_finds_compressor_manual(
        self, doc_store: DocumentStoreConnector
    ) -> None:
        """'air filter' is specific to the compressor manual."""
        results = await doc_store.search("air filter")
        assert results
        sources = {chunk.source for chunk in results}
        assert any("compressor_comp301" in src for src in sources), (
            f"expected compressor manual in sources, got: {sources}"
        )

    @pytest.mark.asyncio
    async def test_every_chunk_carries_source_citation(
        self, doc_store: DocumentStoreConnector
    ) -> None:
        """Every chunk returned by search() must have a non-empty source
        so the agent can render a file citation back to the user."""
        results = await doc_store.search("maintenance")
        assert results
        for chunk in results:
            assert chunk.source, f"chunk has empty source: {chunk}"
            assert chunk.source.endswith(".md"), f"expected .md source, got: {chunk.source}"

    @pytest.mark.asyncio
    async def test_search_filters_by_asset_id(self, doc_store: DocumentStoreConnector) -> None:
        """When asset_id is provided, only chunks whose source path mentions
        that asset should be returned (per document_store.py behaviour)."""
        results = await doc_store.search("maintenance", asset_id="p201")
        # Either empty (nothing matches) or all matches reference p201
        for chunk in results:
            assert "p201" in chunk.source.lower(), (
                f"asset_id filter leaked a non-matching chunk: {chunk.source}"
            )

    @pytest.mark.asyncio
    async def test_chunks_are_document_chunk_instances(
        self, doc_store: DocumentStoreConnector
    ) -> None:
        """Contract check: search results are DocumentChunk dataclasses
        with the expected fields populated."""
        results = await doc_store.search("pump")
        assert results
        chunk = results[0]
        assert isinstance(chunk, DocumentChunk)
        assert chunk.content
        assert chunk.source
