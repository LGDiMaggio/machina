"""Tests for BM25 sparse index and Reciprocal Rank Fusion."""

from __future__ import annotations

import pytest

# rank_bm25 is part of the [docs-rag-hybrid] extra. Skip the whole module
# when it is not installed; the connector itself still works without it.
pytest.importorskip("rank_bm25")

from machina.connectors.docs.hybrid import BM25Index, rrf_fuse, tokenize


class TestTokenize:
    """Tokenizer must keep technical identifiers intact."""

    def test_lowercase(self) -> None:
        assert tokenize("SKF Bearing") == ["skf", "bearing"]

    def test_hyphenated_code_kept_as_single_token(self) -> None:
        # "SKF 6310-2RS" splits into two tokens, but "6310-2RS" stays whole.
        assert tokenize("SKF 6310-2RS") == ["skf", "6310-2rs"]

    def test_work_order_id_kept_intact(self) -> None:
        assert tokenize("WO-2026-0087") == ["wo-2026-0087"]

    def test_torque_spec_keeps_number_and_unit(self) -> None:
        assert tokenize("torque 45 Nm") == ["torque", "45", "nm"]

    def test_dotted_version_kept_whole(self) -> None:
        assert tokenize("Spec rev 2.1.0") == ["spec", "rev", "2.1.0"]

    def test_punctuation_split(self) -> None:
        assert tokenize("first, second; third.") == ["first", "second", "third"]

    def test_empty_string(self) -> None:
        assert tokenize("") == []


class TestBM25Index:
    """Sparse retrieval correctness on technical content."""

    def _populated_index(self) -> BM25Index:
        idx = BM25Index()
        idx.add(
            "c1", "Pump P-201 SKF 6310-2RS bearing replacement procedure", {"asset_id": "P-201"}
        )
        idx.add("c2", "Compressor COMP-301 air filter every 2000 hours", {"asset_id": "COMP-301"})
        idx.add("c3", "Generic vibration monitoring guide", {"asset_id": ""})
        idx.add("c4", "Work order WO-2026-0087 closed by technician", {"asset_id": "P-201"})
        idx.build()
        return idx

    def test_empty_index_returns_empty(self) -> None:
        idx = BM25Index()
        idx.build()
        assert idx.search("anything") == []

    def test_empty_query_returns_empty(self) -> None:
        idx = self._populated_index()
        assert idx.search("") == []

    def test_exact_code_match_top1(self) -> None:
        idx = self._populated_index()
        hits = idx.search("SKF 6310-2RS")
        assert hits, "expected a hit for an exact code"
        assert hits[0][0] == "c1"

    def test_work_order_id_match(self) -> None:
        idx = self._populated_index()
        hits = idx.search("WO-2026-0087")
        assert hits and hits[0][0] == "c4"

    def test_filter_scopes_results(self) -> None:
        idx = self._populated_index()
        hits = idx.search("SKF 6310-2RS", filters={"asset_id": "COMP-301"})
        # P-201 chunk would have matched but the filter excludes it; no
        # other chunk mentions SKF/6310-2RS so we expect zero hits.
        assert hits == []

    def test_filter_keeps_matching_asset(self) -> None:
        idx = self._populated_index()
        hits = idx.search("bearing", filters={"asset_id": "P-201"})
        assert hits and hits[0][0] == "c1"

    def test_top_k_truncates(self) -> None:
        idx = self._populated_index()
        hits = idx.search("procedure bearing filter vibration", k=2)
        assert len(hits) <= 2

    def test_len_reports_staged_chunks(self) -> None:
        idx = BM25Index()
        idx.add("a", "one two three")
        idx.add("b", "four five six")
        assert len(idx) == 2


class TestRRFFuse:
    """Reciprocal Rank Fusion combines independent rankings."""

    def test_single_ranking_passthrough_preserves_order(self) -> None:
        ranking = [("a", 5.0), ("b", 4.0), ("c", 1.0)]
        out = rrf_fuse([ranking])
        assert [chunk_id for chunk_id, _ in out] == ["a", "b", "c"]

    def test_chunk_in_both_rankings_outranks_singletons(self) -> None:
        dense = [("a", 0.9), ("b", 0.8)]
        sparse = [("a", 5.0), ("c", 1.0)]
        out = rrf_fuse([dense, sparse])
        # 'a' is rank-1 in both → must beat 'b' and 'c'.
        assert out[0][0] == "a"

    def test_score_uses_rank_not_score(self) -> None:
        # RRF is score-agnostic: only positions matter. When two lists
        # rank the same items in mirrored order, the fused scores tie
        # for both items.
        r1 = [("a", 0.01), ("b", 999.0)]  # a@1, b@2
        r2 = [("b", 0.01), ("a", 999.0)]  # b@1, a@2
        out = rrf_fuse([r1, r2])
        ids = {chunk_id for chunk_id, _ in out}
        assert ids == {"a", "b"}
        scores = sorted(score for _, score in out)
        assert scores[0] == scores[1]

    def test_empty_rankings_returns_empty(self) -> None:
        assert rrf_fuse([]) == []
        assert rrf_fuse([[], []]) == []

    def test_k_constant_affects_dampening(self) -> None:
        ranking = [("a", 1.0)]
        small_k = rrf_fuse([ranking], k=1)[0][1]
        large_k = rrf_fuse([ranking], k=1000)[0][1]
        # Smaller k means rank-1 contribution is larger.
        assert small_k > large_k


class TestHybridIntegrationKeywordFallback:
    """End-to-end keyword-fallback path with BM25 added on top.

    These tests don't require Chroma — they exercise the BM25 index in
    isolation against realistic technical content to prove the value
    proposition (exact-match recall on codes / numeric specs).
    """

    def test_exact_code_outranks_semantically_similar(self) -> None:
        idx = BM25Index()
        idx.add(
            "c-procedure",
            "Bearing replacement procedure: lock out the motor, remove coupling, use puller.",
        )
        idx.add("c-spec", "Pump P-201 uses SKF 6310-2RS bearing. Replace every 5000 hours.")
        idx.add("c-other", "Air compressor maintenance schedule.")
        idx.build()
        hits = idx.search("SKF 6310-2RS replacement")
        assert hits[0][0] == "c-spec"

    def test_numeric_torque_spec(self) -> None:
        idx = BM25Index()
        idx.add("c1", "Tighten head bolts to 45 Nm in star pattern.")
        idx.add("c2", "Tighten housing bolts to 12 Nm.")
        idx.add("c3", "General torque guidance: follow OEM specs.")
        idx.build()
        hits = idx.search("45 Nm head bolt")
        assert hits[0][0] == "c1"
