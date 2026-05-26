"""Hybrid retrieval helpers — BM25 sparse index + Reciprocal Rank Fusion.

The dense embedding side (Chroma) is good at "what does this mean" — it
finds chunks that *paraphrase* the query. It is bad at exact-match
recall: codes like ``SKF 6310-2RS``, work-order IDs like
``WO-2026-0087``, and numeric specs like ``45 Nm`` are precisely where
dense embeddings struggle. BM25 fills that gap.

This module hosts:

- :func:`tokenize` — a tokenizer tuned for technical manuals: keeps
  hyphens, dots, and digits inside tokens so identifiers stay intact.
- :class:`BM25Index` — a thin in-process wrapper over ``rank_bm25``,
  with optional metadata filtering at query time.
- :func:`rrf_fuse` — Reciprocal Rank Fusion of multiple ranked lists,
  the standard parameter-free way to combine dense and sparse scores.

The heavy dependency ``rank_bm25`` is imported lazily inside
:meth:`BM25Index.build` so importing this module without the extra
installed still succeeds and lets a connector probe the capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Token pattern: a token starts with any Unicode letter or digit (``\w``
# minus underscore), and continues with letters/digits/hyphens/dots/
# underscores. ``\w`` is Unicode-aware by default in Python 3, so CJK,
# Cyrillic, Arabic, Greek, etc. tokenize correctly. Inner punctuation
# (dots, hyphens, underscores) is preserved so technical identifiers
# like ``6310-2RS`` and ``2.1.0`` stay whole.
_TOKEN_RE = re.compile(r"[^\W_][\w.\-]*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase + alphanumeric-with-hyphen-and-dot Unicode tokenizer.

    Designed so technical identifiers survive intact:

    >>> tokenize("Replace SKF 6310-2RS bearing, torque 45 Nm")
    ['replace', 'skf', '6310-2rs', 'bearing', 'torque', '45', 'nm']

    >>> tokenize("WO-2026-0087")
    ['wo-2026-0087']

    Unicode-aware: non-ASCII scripts also tokenize.

    >>> tokenize("Замена подшипника")  # Russian
    ['замена', 'подшипника']
    """
    # Strip trailing dots, hyphens, and underscores so sentence punctuation
    # ("third.") does not stick to the token while inner punctuation
    # ("2.1.0", "WO-2026-0087") is preserved.
    return [tok.rstrip("._-") for tok in _TOKEN_RE.findall(text.lower())]


@dataclass
class _BM25Entry:
    chunk_id: str
    tokens: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


class BM25Index:
    """In-process BM25 sparse retriever.

    The index is built once at :meth:`build` time and queried with
    :meth:`search`. ``rank_bm25`` is imported lazily so callers can
    catch ``ImportError`` and degrade to dense-only mode when the
    ``[docs-rag-hybrid]`` extra is not installed.

    Example:
        ```python
        idx = BM25Index()
        idx.add("c1", "Pump P-201 SKF 6310-2RS bearing", {"asset_id": "P-201"})
        idx.add("c2", "Compressor COMP-301 air filter", {"asset_id": "COMP-301"})
        idx.build()
        hits = idx.search("SKF 6310-2RS", k=5)
        # [("c1", 1.23), ...]
        ```
    """

    def __init__(self) -> None:
        self._entries: list[_BM25Entry] = []
        self._bm25: Any = None

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, chunk_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Stage a chunk for indexing. Call :meth:`build` once all chunks are added."""
        self._entries.append(
            _BM25Entry(chunk_id=chunk_id, tokens=tokenize(text), metadata=metadata or {})
        )

    def build(self) -> None:
        """Construct the underlying ``BM25Okapi`` instance.

        Raises:
            ImportError: when ``rank_bm25`` is not installed. Callers
                typically catch this and fall back to dense-only mode.
        """
        if not self._entries:
            self._bm25 = None
            return
        from rank_bm25 import BM25Okapi  # type: ignore[import-not-found,unused-ignore]

        self._bm25 = BM25Okapi([entry.tokens for entry in self._entries])

    def search(
        self,
        query: str,
        *,
        k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        """Return up to ``k`` ``(chunk_id, score)`` tuples ranked by BM25.

        Args:
            query: User query string. Tokenized with :func:`tokenize`.
            k: Maximum number of results to return after filtering.
            filters: Optional metadata constraint applied before scoring;
                only chunks matching every key/value pair are scored. Same
                semantics as the connector's ``filters=`` kwarg.

        Returns:
            ``[(chunk_id, score)]`` sorted by score descending. Empty when
            the index is empty or the tokenized query matches no terms.
        """
        if self._bm25 is None or not self._entries:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        scored: list[tuple[str, float]] = []
        for entry, score in zip(self._entries, scores, strict=True):
            if score <= 0.0:
                continue
            if filters and not _matches_filters(entry.metadata, filters):
                continue
            scored.append((entry.chunk_id, float(score)))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


def _matches_filters(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    return all(metadata.get(key) == value for key, value in filters.items())


def rrf_fuse(
    rankings: list[list[tuple[str, float]]],
    *,
    k: int = 60,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over multiple ranked lists.

    For each chunk_id, accumulate ``1 / (k + rank)`` across every list
    where it appears (rank is 1-based). The constant ``k=60`` is the
    standard choice from Cormack et al. 2009 and works well without
    tuning.

    Args:
        rankings: List of ranked lists, each ``[(chunk_id, score)]`` in
            descending score order. Scores are ignored — only ranks are
            used, which is what makes RRF parameter-free.
        k: RRF dampening constant. Larger ``k`` softens the contribution
            of top ranks; 60 is the literature default.

    Returns:
        Fused ranking ``[(chunk_id, fused_score)]`` sorted by score desc.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (chunk_id, _score) in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    out = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
    return out
