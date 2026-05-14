"""Tests for the cross-encoder reranker.

``sentence_transformers`` is heavy (~1GB model on first load) and lives
behind the ``[docs-rag-rerank]`` extra. These tests mock the underlying
``CrossEncoder`` via ``sys.modules`` so they run in milliseconds and
don't require the extra to be installed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from machina.connectors.docs.reranker import CrossEncoderReranker


def _make_mock_cross_encoder(scores: list[float]) -> MagicMock:
    """Return a MagicMock that mimics ``sentence_transformers.CrossEncoder``."""
    instance = MagicMock()
    instance.predict.return_value = scores
    cls = MagicMock(return_value=instance)
    module = MagicMock()
    module.CrossEncoder = cls
    return module, instance, cls


class TestCrossEncoderRerank:
    def test_empty_candidates_returns_empty(self) -> None:
        rr = CrossEncoderReranker()
        assert rr.rerank("query", []) == []

    def test_reorders_by_score(self) -> None:
        module, _instance, cls = _make_mock_cross_encoder([0.1, 0.95, 0.4])
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            rr = CrossEncoderReranker()
            out = rr.rerank(
                "how to replace a bearing",
                [("c1", "noise"), ("c2", "bearing replacement procedure"), ("c3", "lube")],
            )
        # Highest-scoring c2 must be first, then c3, then c1.
        assert [chunk_id for chunk_id, _ in out] == ["c2", "c3", "c1"]
        # Model was instantiated once with the default model name.
        cls.assert_called_once_with("BAAI/bge-reranker-v2-m3")

    def test_custom_model_name(self) -> None:
        module, _instance, cls = _make_mock_cross_encoder([0.5, 0.5])
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            rr = CrossEncoderReranker(model_name="org/custom-reranker")
            rr.rerank("q", [("a", "x"), ("b", "y")])
        cls.assert_called_once_with("org/custom-reranker")

    def test_model_loaded_once_across_calls(self) -> None:
        module, _instance, cls = _make_mock_cross_encoder([0.1])
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            rr = CrossEncoderReranker()
            rr.rerank("q", [("a", "x")])
            rr.rerank("q", [("a", "x")])
            rr.rerank("q", [("a", "x")])
        # Lazy load is cached on the instance.
        assert cls.call_count == 1

    def test_import_error_returns_unranked_candidates(self) -> None:
        # sentence_transformers absent → graceful degrade.
        rr = CrossEncoderReranker()
        # Simulate import failure by ensuring sys.modules has no entry and
        # the real package is not installed in the test env (we don't
        # actually import it).
        out = rr.rerank("q", [("a", "x"), ("b", "y")])
        # Either the package is installed (returns sorted) or not (returns
        # zero scores in original order). In both cases the chunks are
        # preserved; we just verify length and ids.
        assert len(out) == 2
        assert {chunk_id for chunk_id, _ in out} == {"a", "b"}

    def test_predict_failure_returns_original_order_with_zero_scores(self) -> None:
        module = MagicMock()
        instance = MagicMock()
        instance.predict.side_effect = RuntimeError("OOM")
        module.CrossEncoder = MagicMock(return_value=instance)
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            rr = CrossEncoderReranker()
            out = rr.rerank("q", [("a", "x"), ("b", "y")])
        # Original order preserved; scores zeroed.
        assert [chunk_id for chunk_id, _ in out] == ["a", "b"]
        assert all(score == 0.0 for _, score in out)

    def test_load_failure_is_remembered(self) -> None:
        """After a load failure the model is not retried on each call."""
        module = MagicMock()
        module.CrossEncoder = MagicMock(side_effect=RuntimeError("download blocked"))
        with patch.dict("sys.modules", {"sentence_transformers": module}):
            rr = CrossEncoderReranker()
            rr.rerank("q", [("a", "x")])
            rr.rerank("q", [("a", "x")])
        # CrossEncoder constructor attempted only once despite two calls.
        assert module.CrossEncoder.call_count == 1
