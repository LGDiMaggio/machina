"""Minimal golden-set retrieval eval for the RAG pipeline.

The RAG upgrade shipped without a structured eval set (an explicitly
accepted risk in the brainstorm). This is the smallest thing that turns
the five observed failure modes into a *repeatable, regression-detectable*
check, rather than a one-time qualitative spot-check:

* failure 1 — exact-code / part-number misses (SKF 6310-2RS, FILTER-GA55-INT)
* failure 2 — numeric spec inside a table (torque 45 Nm) lost or linearised
* failure 3 — step-by-step procedure split across chunks
* failure 4 — cross-asset false positives (P-201 query returning P-105/COMP)

It is NOT a full RAGAS-style harness — just a fixed corpus, a frozen list
of ``(query, filter, expected-substring)`` rows, and a single index build.
Gated on the ``[docs-rag-hybrid]`` stack so a stripped-down environment
skips instead of failing.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

# The exact-code rows below rely on the BM25 (sparse) signal, so the whole
# module is gated on the hybrid stack. Without it, dense-only retrieval
# cannot be expected to surface exact identifiers and the eval is invalid.
pytest.importorskip("langchain_community")
pytest.importorskip("langchain_chroma")
pytest.importorskip("chromadb")
pytest.importorskip("rank_bm25")

from machina.connectors.docs.document_store import DocumentStoreConnector

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


_P201 = (
    "---\n"
    "asset_id: P-201\n"
    "doc_type: procedure\n"
    "equipment_class_code: PU\n"
    "---\n"
    "# Pump P-201 Manual\n\n"
    "## Bearing Replacement Procedure\n\n"
    "Step 1: Lock out the motor.\n"
    "Step 2: Remove coupling using an SKF 6310-2RS bearing extractor.\n"
    "Step 3: Heat new SKF 6310-2RS bearings to 110 C.\n"
    "Step 4: Slide bearings onto the shaft.\n"
    "Step 5: Reassemble the coupling and torque the head bolt to 45 Nm.\n\n"
    "## Torque Specifications\n\n"
    "| Fastener | Torque |\n"
    "| --- | --- |\n"
    "| Head bolt M12 | 45 Nm |\n"
    "| Coupling bolt M8 | 22 Nm |\n"
)

# A second pump so cross-asset filtering has something to wrongly match.
_P105 = (
    "---\n"
    "asset_id: P-105\n"
    "doc_type: procedure\n"
    "equipment_class_code: PU\n"
    "---\n"
    "# Pump P-105 Manual\n\n"
    "## Bearing Replacement Procedure\n\n"
    "Step 1: Isolate the pump.\n"
    "Step 2: Extract the worn bearings.\n"
    "Step 3: Fit new bearings and torque the head bolt to 60 Nm.\n"
)

_COMP301 = (
    "---\n"
    "asset_id: COMP-301\n"
    "doc_type: manual\n"
    "equipment_class_code: CO\n"
    "---\n"
    "# Compressor COMP-301 Manual\n\n"
    "## Filter Replacement\n\n"
    "Replace the COMP-301 intake air filter every 2000 hours.\n"
    "Part number FILTER-GA55-INT.\n"
)


# Frozen golden rows. Each: (id, query, filters, top_k, expected_substring).
# A row passes when expected_substring appears in the content of any chunk
# in the returned top_k. Keep this list small and high-signal — it is a
# tripwire, not an exhaustive benchmark.
_GOLDEN: list[tuple[str, str, dict[str, str] | None, int, str]] = [
    # failure 1: exact bearing code must be retrievable (BM25 signal).
    ("exact_bearing_code", "SKF 6310-2RS", None, 5, "SKF 6310-2RS"),
    # failure 1: exact part number for the compressor filter.
    ("exact_part_number", "FILTER-GA55-INT", None, 5, "FILTER-GA55-INT"),
    # failure 2: numeric spec that lives inside a table row.
    ("torque_spec_table", "head bolt torque specification", {"asset_id": "P-201"}, 5, "45 Nm"),
    # failure 3: full step-by-step procedure returned intact (parent expansion).
    ("procedure_integrity", "how to replace the bearing", {"asset_id": "P-201"}, 3, "Step 1"),
    (
        "procedure_integrity_end",
        "bearing replacement procedure",
        {"asset_id": "P-201"},
        3,
        "Step 5",
    ),
]


async def test_golden_retrieval_set(tmp_path: Path) -> None:
    """Every frozen golden row retrieves its expected content in top_k."""
    docs_dir = tmp_path / "manuals"
    docs_dir.mkdir()
    (docs_dir / "P-201_pump.md").write_text(_P201, encoding="utf-8")
    (docs_dir / "P-105_pump.md").write_text(_P105, encoding="utf-8")
    (docs_dir / "COMP-301_compressor.md").write_text(_COMP301, encoding="utf-8")

    conn = DocumentStoreConnector(
        paths=[docs_dir],
        collection_name=f"golden_{uuid.uuid4().hex[:8]}",
    )
    await conn.connect()
    health = await conn.health_check()
    assert health.details.get("mode") == "rag", health.details

    failures: list[str] = []
    for row_id, query, filters, top_k, expected in _GOLDEN:
        results = await conn.search(query, filters=filters, top_k=top_k)
        haystack = "\n".join(r.content for r in results)
        if expected not in haystack:
            failures.append(
                f"[{row_id}] query={query!r} filters={filters} "
                f"expected {expected!r} in top-{top_k}; "
                f"got sources={[getattr(r, 'source', '?') for r in results]}"
            )

    assert not failures, "golden retrieval regressions:\n" + "\n".join(failures)


async def test_golden_cross_asset_isolation(tmp_path: Path) -> None:
    """failure 4: a metadata-filtered query never leaks another asset's chunks."""
    docs_dir = tmp_path / "manuals"
    docs_dir.mkdir()
    (docs_dir / "P-201_pump.md").write_text(_P201, encoding="utf-8")
    (docs_dir / "P-105_pump.md").write_text(_P105, encoding="utf-8")
    (docs_dir / "COMP-301_compressor.md").write_text(_COMP301, encoding="utf-8")

    conn = DocumentStoreConnector(
        paths=[docs_dir],
        collection_name=f"golden_iso_{uuid.uuid4().hex[:8]}",
    )
    await conn.connect()

    # A bearing query (semantically matches both pumps) filtered to P-105
    # must return only P-105 chunks — no P-201 / COMP-301 bleed-through.
    results = await conn.search(
        "bearing replacement torque", filters={"asset_id": "P-105"}, top_k=5
    )
    assert results, "expected at least one P-105 chunk"
    leaked = [r.asset_id for r in results if r.asset_id != "P-105"]
    assert not leaked, f"cross-asset filter leaked non-P-105 chunks: {leaked}"
