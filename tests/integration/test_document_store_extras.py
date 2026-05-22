"""Integration smoke test for the [docs-rag-pro] stack.

Skipped automatically when any of the heavy dependencies in the
aggregator extra is missing. Exercises the cross-layer flow that
unit tests can only mock:

* sidecar metadata → connector indexing
* hybrid retrieval (dense + BM25) with metadata filter applied
  pre-retrieval
* cross-encoder reranker reordering the fused candidates
* section-aware chunking returning the full parent section

No agent runtime, no LLM — that wiring is exercised in
``test_knowledge_agent_manuals.py``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

# Gate the whole module on the heavy dependencies. ``importorskip`` makes
# the test invisible when any of them is missing instead of failing CI on
# a stripped-down environment.
pytest.importorskip("langchain_community")
pytest.importorskip("chromadb")
pytest.importorskip("rank_bm25")

from machina.connectors.docs.document_store import DocumentStoreConnector

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.asyncio


async def test_hybrid_retrieval_with_metadata_filter_and_rerank(
    tmp_path: Path,
) -> None:
    """End-to-end: ingest → hybrid query → metadata filter → reranker → parent expansion."""
    docs_dir = tmp_path / "manuals"
    docs_dir.mkdir()

    p201 = docs_dir / "P-201_pump.md"
    p201.write_text(
        "---\n"
        "asset_id: P-201\n"
        "doc_type: procedure\n"
        "equipment_class_code: PU\n"
        "---\n"
        "# Pump P-201 Manual\n\n"
        "## Bearing Replacement Procedure\n\n"
        "Step 1: Lock out the motor.\n"
        "Step 2: Remove coupling using SKF 6310-2RS extractor.\n"
        "Step 3: Heat new SKF 6310 bearings to 110 C.\n"
        "Step 4: Slide bearings onto shaft.\n"
        "Step 5: Reassemble coupling.\n"
        "\n"
        "## Vibration Limits\n\n"
        "DE less than 4.5 mm/s, NDE less than 3.5 mm/s.\n",
        encoding="utf-8",
    )

    comp301 = docs_dir / "COMP-301_compressor.md"
    comp301.write_text(
        "---\n"
        "asset_id: COMP-301\n"
        "doc_type: manual\n"
        "equipment_class_code: CO\n"
        "---\n"
        "# Compressor COMP-301 Manual\n\n"
        "## Filter Replacement\n\n"
        "Replace COMP-301 intake air filter every 2000 hours.\n"
        "Part number FILTER-GA55-INT.\n",
        encoding="utf-8",
    )

    # Unique collection name so we don't share in-memory Chroma state
    # with other tests in this process (Chroma reuses collections by
    # name within the default ephemeral client).
    conn = DocumentStoreConnector(
        paths=[docs_dir],
        collection_name=f"integ_{uuid.uuid4().hex[:8]}",
    )
    await conn.connect()

    # Health-check confirms we landed on the RAG path, not the keyword
    # fallback. If this fails the rest of the assertions are meaningless.
    health = await conn.health_check()
    assert health.details.get("mode") == "rag", health.details

    # Hybrid query: technical identifier "SKF 6310-2RS" is the BM25
    # signal; "bearing replacement" is the dense signal. Filter on
    # asset_id so the compressor doc is excluded pre-retrieval.
    results = await conn.search(
        "SKF 6310-2RS bearing replacement",
        filters={"asset_id": "P-201"},
        top_k=2,
    )

    assert results, "expected at least one match for the P-201 procedure"
    assert all(r.asset_id == "P-201" for r in results), (
        "metadata filter must exclude COMP-301 results: "
        f"{[(r.asset_id, r.source) for r in results]}"
    )

    # Parent expansion: the winning chunk should contain the full
    # multi-step procedure, not just the chunk that matched.
    top = results[0]
    assert "Step 1" in top.content and "Step 5" in top.content, (
        f"parent expansion did not include the full procedure: {top.content[:200]!r}"
    )
    # The citation contract carries the section title surfaced by the splitter.
    assert top.section_title == "Bearing Replacement Procedure"


async def test_reranker_runs_on_top_of_hybrid_fusion(tmp_path: Path) -> None:
    """Smoke-check that wiring a reranker_model doesn't break the hybrid path.

    Gated on ``sentence_transformers`` since loading a cross-encoder is
    the only step that requires it; the rest of the pro stack runs in
    the test above. The first invocation also exercises the lazy
    model-load path inside :class:`CrossEncoderReranker`.
    """
    pytest.importorskip("sentence_transformers")

    docs_dir = tmp_path / "manuals"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text(
        "# Bearing Replacement\n\n"
        "Step 1: Lock out.\nStep 2: Pull bearings.\nStep 3: Heat new bearings.\n",
        encoding="utf-8",
    )

    conn = DocumentStoreConnector(
        paths=[docs_dir],
        reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        collection_name=f"integ_rerank_{uuid.uuid4().hex[:8]}",
    )
    await conn.connect()
    results = await conn.search("how do I replace a bearing", top_k=1)
    assert results, "reranker path should still surface a match"
    # Parent expansion preserved.
    assert "Step 1" in results[0].content
