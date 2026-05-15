"""Cross-encoder reranking — the highest-ROI quality step after hybrid retrieval.

Dense + sparse + RRF fusion finds *candidates*. A cross-encoder is much
better at deciding *which of the candidates actually answers the query*
because it scores each (query, chunk) pair jointly rather than comparing
independent embeddings. The standard pipeline:

    retrieve top-30 (hybrid) → rerank → keep top-5 for the LLM

The default model is ``BAAI/bge-reranker-v2-m3`` (open-source,
multilingual, CPU-friendly). It loads on first use and is reused across
calls. When the ``[docs-rag-rerank]`` extra is not installed, callers
catch :class:`ImportError` and degrade to the un-reranked order.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class CrossEncoderReranker:
    """Lazy-loaded cross-encoder for query/chunk relevance scoring.

    The model itself is only imported and instantiated on the first
    :meth:`rerank` call so importing this module is cheap and a
    connector can probe for the capability without paying the model
    download / load cost.

    Args:
        model_name: HuggingFace model identifier. Defaults to
            ``BAAI/bge-reranker-v2-m3``. Commercial alternatives
            (Cohere Rerank 3) can be wired via a different subclass.

    Example:
        ```python
        rr = CrossEncoderReranker()
        scored = rr.rerank(
            "how to replace a bearing",
            [("c1", "Bearing replacement procedure ..."),
             ("c2", "Compressor maintenance schedule ...")],
        )
        # [("c1", 0.94), ("c2", 0.12)]
        ```
    """

    def __init__(self, model_name: str = DEFAULT_RERANKER_MODEL) -> None:
        self.model_name = model_name
        self._model: Any = None
        self._load_failed = False

    def _load(self) -> Any:
        """Import and instantiate the underlying CrossEncoder on demand.

        Returns ``None`` if loading fails (missing extra, model not
        cached, network unavailable). Callers must handle ``None`` and
        degrade gracefully.
        """
        if self._model is not None:
            return self._model
        if self._load_failed:
            return None
        try:
            from sentence_transformers import (
                CrossEncoder,  # type: ignore[import-not-found,unused-ignore]
            )
        except ImportError:
            logger.info(
                "reranker_unavailable",
                operation="load_reranker",
                model=self.model_name,
                hint="Install machina-ai[docs-rag-rerank] for cross-encoder reranking",
            )
            self._load_failed = True
            return None
        try:
            self._model = CrossEncoder(self.model_name)
        except Exception as exc:
            logger.warning(
                "reranker_load_failed",
                operation="load_reranker",
                model=self.model_name,
                error=str(exc),
            )
            self._load_failed = True
            return None
        return self._model

    def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],
    ) -> list[tuple[str, float]] | None:
        """Score every ``(chunk_id, text)`` pair against ``query``.

        Args:
            query: The user query.
            candidates: ``[(chunk_id, chunk_text)]`` to score. Empty
                input returns an empty list.

        Returns:
            ``[(chunk_id, score)]`` sorted by relevance score
            descending. Returns ``None`` when the model is unavailable
            or scoring fails so the caller can preserve the upstream
            ordering and scores instead of overwriting them with a
            zero sentinel.
        """
        if not candidates:
            return []
        model = self._load()
        if model is None:
            return None

        try:
            pairs = [(query, text) for _, text in candidates]
            scores = model.predict(pairs)
        except Exception as exc:
            logger.warning(
                "reranker_predict_failed",
                operation="rerank",
                model=self.model_name,
                candidate_count=len(candidates),
                error=str(exc),
            )
            return None

        scored = [
            (chunk_id, float(score))
            for (chunk_id, _), score in zip(candidates, scores, strict=True)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored
