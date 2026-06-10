"""DocumentStoreConnector — PDF/DOCX ingestion with RAG retrieval.

Uses LangChain document loaders for parsing and ChromaDB for vector
storage.  Falls back to a simple keyword search when RAG dependencies
are not installed.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.connectors.docs.chunking import (
    MatchChunk,
    ParentSection,
    SectionAwareSplitter,
)
from machina.connectors.docs.metadata import DocumentMetadata, strip_frontmatter
from machina.connectors.docs.parsing import LayoutAwareParser
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


# Over-fetch multiplier so parent dedup in _expand_to_parents can still
# honour the caller's top_k after collapsing matches that share a parent.
_PARENT_OVERFETCH_FACTOR = 6

# Bump when the chroma_meta schema changes (e.g. when new chunk fields
# are added that retrieval depends on). The connector appends this to
# ``collection_name`` so a persistent v0.2 collection doesn't get
# mixed with v0.3+ reads that expect ``parent_id`` / ``start_offset``
# / ``is_table`` in metadata.
_SCHEMA_VERSION = "v3"


class _IndexedChunk(NamedTuple):
    """Tuple returned by ``_iter_doc_chunks`` — one entry per indexed chunk.

    Named so callers don't have to read by tuple position; both the RAG
    index builder and the keyword builder iterate the same shape.
    """

    chunk_id: str
    text: str
    chroma_meta: dict[str, Any]
    chunk: DocumentChunk


@dataclass
class DocumentChunk:
    """A retrieved passage from a document.

    Since v0.3, ``content`` carries the **full parent section** after
    parent-document retrieval rather than the small match passage that
    was embedded. The match passage is still what the embedder /
    BM25 / reranker scored — only the surface returned to the caller
    expands to its parent so the LLM sees the full surrounding
    context. Callers that previously sliced ``content`` for a short
    passage should switch to keying on ``chunk_id`` instead.

    Args:
        content: Text content of the chunk — typically the full parent
            section the matched passage was nested under.
        source: File path or document name.
        page: Page number (if available).
        score: Relevance score from the retriever.
        chunk_id: Deterministic identifier for this chunk.
        asset_id: Domain asset id (e.g. ``"P-201"``) if known.
        equipment_class_code: ISO 14224 Annex A code if known. Internal use.
        doc_type: One of ``manual``, ``procedure``, ``datasheet``,
            ``troubleshooting``, ``other``.
        section_title: Title of the section this chunk belongs to.
        metadata: Raw metadata bag from the underlying document loader.
        parent_id: Identifier of the section this chunk's match was
            extracted from. Joins back to a :class:`ParentSection`.
        start_offset: Character offset of the match inside its parent
            section body. Used for windowing oversized parents without
            a fragile substring search.
        is_table: ``True`` when this chunk represents an atomic table
            block extracted by the layout-aware parser. Retrieval and
            chunking never split it mid-row; the LLM prompt surfaces a
            ``[TABLE]`` tag so the model treats the content as
            structured rows / columns.

    Example:
        ```python
        from machina.connectors.docs.document_store import DocumentChunk

        chunk = DocumentChunk("Pump P-201 bearing procedure", source="manual.pdf", page=42)
        ```
    """

    content: str
    source: str = ""
    page: int = 0
    score: float = 0.0
    chunk_id: str = ""
    asset_id: str = ""
    equipment_class_code: str = ""
    doc_type: str = ""
    section_title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # New fields added in v0.3 — placed at the end so positional
    # construction in user code keeps working unchanged.
    parent_id: str = ""
    start_offset: int = 0
    is_table: bool = False

    def __repr__(self) -> str:
        preview = self.content[:60] + "..." if len(self.content) > 60 else self.content
        return f"DocumentChunk(source={self.source!r}, page={self.page}, text={preview!r})"


class DocumentStoreConnector:
    """Connector for local PDF/DOCX documents with RAG retrieval.

    Ingests documents from one or more directories, splits them into
    chunks, embeds them in a vector store, and provides semantic search.

    When ``langchain`` and ``chromadb`` are not installed, falls back to
    a simple in-memory keyword search so the quickstart works without
    heavy dependencies.

    Each ingested file can carry structured metadata via a sidecar
    ``<file>.meta.yaml`` or YAML frontmatter (for ``.md`` / ``.txt``).
    Metadata fields (``asset_id``, ``equipment_class_code``, ``doc_type``,
    ``section_title``) are indexed and can be used to filter the search
    space *before* retrieval via the ``filters=`` kwarg.

    Args:
        paths: List of directories or files to ingest.
        collection_name: Name for the ChromaDB collection. ``connect()``
            resets this collection so it contains exactly the connector's
            corpus — pointing two live connector instances at the same
            collection name (in one process, or via a shared persistent
            client) is unsupported: whichever connects last wipes and
            replaces the other's vectors.
        chunk_size: Target size for text chunks (in characters).
        chunk_overlap: Overlap between consecutive chunks.
        reranker_model: Optional ``sentence-transformers`` cross-encoder
            model name to rerank fused results (e.g.
            ``"BAAI/bge-reranker-base"``). Requires the
            ``[docs-rag-rerank]`` extra.
        embedder: Optional ``sentence-transformers`` model name used to
            embed chunks into Chroma. When set (e.g. ``"BAAI/bge-m3"``),
            requires the ``[docs-rag-rerank]`` extra (which pulls in
            ``sentence-transformers``). If the model fails to load —
            extra absent, model not downloaded, GPU/CPU issue — the
            connector silently falls back to Chroma's default embedder
            so ingest does not crash.

    Example:
        ```python
        docs = DocumentStoreConnector(
            paths=["manuals/", "procedures/"],
            embedder="BAAI/bge-m3",
            reranker_model="BAAI/bge-reranker-base",
        )
        await docs.connect()
        results = await docs.search(
            "bearing replacement", filters={"asset_id": "P-201"}
        )
        for chunk in results:
            print(f"[{chunk.source} p.{chunk.page}] {chunk.content[:100]}")
        ```
    """

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.SEARCH_DOCUMENTS, Capability.RETRIEVE_SECTION}
    )

    def __init__(
        self,
        *,
        paths: list[str | Path] | None = None,
        collection_name: str = "machina_docs",
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        reranker_model: str | None = None,
        embedder: str | None = None,
    ) -> None:
        self._paths = [Path(p) for p in (paths or [])]
        # Append the schema sentinel so persistent collections don't
        # outlive a metadata-schema change. Idempotent if the caller
        # already passed a versioned name.
        self._collection_name = (
            collection_name
            if collection_name.endswith(f"_{_SCHEMA_VERSION}")
            else f"{collection_name}_{_SCHEMA_VERSION}"
        )
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._reranker_model_name = reranker_model
        self._embedder_model_name = embedder
        self._connected = False
        self._use_rag = False

        # Splitter is retained so _expand_to_parents can call
        # window_parent for oversized-section truncation using the
        # splitter's max_parent_chars / parent_window contract.
        self._splitter = SectionAwareSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        # Layout-aware parser is constructed eagerly but the underlying
        # Docling import stays lazy — when the [docs-rag-parsing] extra
        # is absent the parser will return None on every parse() call
        # and loaders fall back to PyPDFLoader / Docx2txtLoader.
        self._parser = LayoutAwareParser()

        # Populated after connect()
        self._chunks: list[DocumentChunk] = []
        self._vectorstore: Any = None
        self._bm25_index: Any = None  # BM25Index when [docs-rag-hybrid] installed
        self._chunk_by_id: dict[str, DocumentChunk] = {}
        self._parent_by_id: dict[str, ParentSection] = {}
        self._reranker: Any = None  # CrossEncoderReranker when configured

    # ------------------------------------------------------------------
    # Connector lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Ingest documents and build the vector index."""
        # Reset all index state up-front so a retry after a partial
        # connect() failure doesn't double-count chunks or leak stale
        # parents from a previous corpus.
        self._chunks.clear()
        self._chunk_by_id.clear()
        self._parent_by_id.clear()
        self._vectorstore = None
        self._bm25_index = None
        self._reranker = None

        documents = await asyncio.to_thread(self._load_documents)
        if not documents:
            logger.warning(
                "no_documents_found",
                connector="DocumentStoreConnector",
                paths=[str(p) for p in self._paths],
            )

        try:
            # Docling parse + HF embedder load + Chroma index build are
            # heavy synchronous work; offload to a worker thread so the
            # event loop stays responsive during connect().
            await asyncio.to_thread(self._build_rag_index, documents)
            self._use_rag = True
            if self._reranker_model_name:
                from machina.connectors.docs.reranker import CrossEncoderReranker

                self._reranker = CrossEncoderReranker(self._reranker_model_name)
            logger.info(
                "connected",
                connector="DocumentStoreConnector",
                mode="rag",
                document_count=len(documents),
                reranker=bool(self._reranker_model_name),
            )
        except ImportError:
            await asyncio.to_thread(self._build_keyword_index, documents)
            self._use_rag = False
            logger.info(
                "connected",
                connector="DocumentStoreConnector",
                mode="keyword_fallback",
                chunk_count=len(self._chunks),
            )
        self._connected = True

    async def disconnect(self) -> None:
        """Release resources.

        All connect-time state must be cleared so a subsequent connect()
        cannot serve chunks from the previous corpus or keep a stale
        reranker handle.
        """
        self._vectorstore = None
        self._chunks.clear()
        self._chunk_by_id.clear()
        self._parent_by_id.clear()
        self._bm25_index = None
        self._reranker = None
        self._connected = False
        logger.info("disconnected", connector="DocumentStoreConnector")

    async def health_check(self) -> ConnectorHealth:
        """Check connector status."""
        if not self._connected:
            return ConnectorHealth(
                status=ConnectorStatus.UNHEALTHY,
                message="Not connected",
            )
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message="Connected",
            details={
                "mode": "rag" if self._use_rag else "keyword",
                "chunk_count": len(self._chunks),
            },
        )

    # ------------------------------------------------------------------
    # Search operations
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        asset_id: str = "",
        filters: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Search documents for passages relevant to the query.

        Args:
            query: The search query.
            top_k: Maximum number of results to return.
            asset_id: Optional asset ID to scope the search. Shortcut for
                ``filters={"asset_id": asset_id}``.
            filters: Optional metadata filter applied **before** retrieval.
                Supported keys: ``asset_id``, ``equipment_class_code``,
                ``doc_type``, ``section_title``, plus any custom field
                stored in chunk metadata.

        Returns:
            List of relevant document chunks, ranked by relevance.
        """
        self._ensure_connected()
        effective = self._merge_filters(filters, asset_id)
        # Over-fetch so parent dedup in _expand_to_parents can still
        # honour the caller's top_k contract after collapsing matches
        # that share a parent_id. The ceiling is bounded by the corpus
        # so a small corpus doesn't trigger an unbounded fetch — and
        # we log when we still can't fill top_k after collapsing, so
        # callers have a signal that the contract was short.
        corpus_size = len(self._chunks) or top_k
        over_fetch = min(
            corpus_size,
            max(top_k * _PARENT_OVERFETCH_FACTOR, top_k + 50),
        )
        if self._use_rag:
            results = await asyncio.to_thread(
                self._rag_search, query, top_k=over_fetch, filters=effective
            )
        else:
            results = self._keyword_search(query, top_k=over_fetch, filters=effective)
        expanded = self._expand_to_parents(results, top_k=top_k)
        if len(expanded) < top_k:
            logger.info(
                "search_top_k_short",
                connector="DocumentStoreConnector",
                requested=top_k,
                returned=len(expanded),
                candidates=len(results),
                reason="parent_dedup_or_corpus_size",
            )
        return expanded

    async def search_documents(
        self,
        query: str = "",
        *,
        top_k: int = 5,
        asset_id: str = "",
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[DocumentChunk]:
        """Alias for :meth:`search` matching the declared capability name."""
        return await self.search(query, top_k=top_k, asset_id=asset_id, filters=filters)

    async def retrieve_section(
        self,
        source: str,
        page: int,
    ) -> str:
        """Retrieve the full text of a specific page/section.

        Args:
            source: Document source path.
            page: Page number.

        Returns:
            The text content of that section.
        """
        self._ensure_connected()
        for chunk in self._chunks:
            if chunk.source == source and chunk.page == page:
                return chunk.content
        return ""

    # ------------------------------------------------------------------
    # RAG index (LangChain + ChromaDB)
    # ------------------------------------------------------------------

    def _iter_doc_chunks(self, documents: list[dict[str, Any]]) -> list[_IndexedChunk]:
        """Run the splitter over every document and produce indexable chunks.

        Returns a list of :class:`_IndexedChunk` named tuples and
        populates ``self._parent_by_id`` / ``self._chunks`` /
        ``self._chunk_by_id`` as a side effect. Shared by the RAG and
        keyword index builders so both modes index against the same
        chunk shape.
        """
        out: list[_IndexedChunk] = []
        for doc in documents:
            doc_meta: DocumentMetadata = doc.get("doc_metadata") or DocumentMetadata()
            parsed = doc.get("parsed")
            if parsed is not None:
                # Layout-aware parser already identified sections + tables;
                # let the splitter consume that structured payload so
                # tables stay atomic and headings come from the parser
                # rather than regex heuristics.
                parents, matches = self._splitter.split_structured(parsed)
            else:
                parents, matches = self._splitter.split(doc["content"], source=doc["source"])
            for parent in parents:
                self._parent_by_id[parent.parent_id] = parent
            base_page = doc.get("page", 0)
            for i, match in enumerate(matches):
                section_title = match.section_title or doc_meta.section_title
                # Structured path: every match carries the page it came
                # from. Flat-text path: every match inherits the page of
                # the source record (one record per page for PDFs).
                page = match.page or base_page or 0
                chunk_id = _make_chunk_id(
                    source=doc["source"],
                    page=page,
                    section_title=section_title,
                    index=i,
                )
                chroma_meta: dict[str, Any] = {
                    "source": doc["source"],
                    "page": page,
                    "chunk_id": chunk_id,
                    "parent_id": match.parent_id,
                    "start_offset": match.start_offset,
                    "is_table": match.atomic,
                }
                chroma_meta.update(doc_meta.to_chroma_dict())
                if section_title and "section_title" not in chroma_meta:
                    chroma_meta["section_title"] = section_title

                new_chunk = DocumentChunk(
                    content=match.text,
                    source=doc["source"],
                    page=page,
                    chunk_id=chunk_id,
                    parent_id=match.parent_id,
                    start_offset=match.start_offset,
                    is_table=match.atomic,
                    asset_id=doc_meta.asset_id,
                    equipment_class_code=doc_meta.equipment_class_code,
                    doc_type=doc_meta.doc_type,
                    section_title=section_title,
                    metadata=chroma_meta,
                )
                self._chunks.append(new_chunk)
                self._chunk_by_id[chunk_id] = new_chunk
                out.append(
                    _IndexedChunk(
                        chunk_id=chunk_id,
                        text=match.text,
                        chroma_meta=chroma_meta,
                        chunk=new_chunk,
                    )
                )
        return out

    def _build_rag_index(self, documents: list[dict[str, Any]]) -> None:
        """Build a ChromaDB vector store from parsed documents.

        Drops any pre-existing collection with our name first:
        ``Chroma.from_texts`` get-or-creates and ADDs, and Chroma clients
        share state by collection name (one in-process system for the
        default ephemeral client, on-disk state for persistent ones), so
        without the reset a reconnect after a corpus change — or a second
        connector instance reusing the name — would serve stale or
        foreign vectors that ``_expand_to_parents`` then returns as
        orphan match-chunks with valid-looking citations. connect() must
        establish the invariant: the collection contains exactly this
        corpus.
        """
        from langchain_community.vectorstores import (  # type: ignore[import-not-found,unused-ignore]
            Chroma,
        )

        try:
            # The bare constructor get-or-creates the collection, so
            # delete_collection() always has something to delete. A
            # failed reset would silently break the exactly-this-corpus
            # invariant, so it is fatal — except ImportError (chromadb
            # missing), which must propagate for the keyword fallback.
            Chroma(collection_name=self._collection_name).delete_collection()
        except ImportError:
            raise
        except Exception as exc:
            raise ConnectorError(f"Chroma collection reset failed: {exc!r}") from exc

        indexed = self._iter_doc_chunks(documents)
        if not indexed:
            return
        ids = [item.chunk_id for item in indexed]
        texts = [item.text for item in indexed]
        metadatas = [item.chroma_meta for item in indexed]

        from_texts_kwargs: dict[str, Any] = {
            "texts": texts,
            "metadatas": metadatas,
            "ids": ids,
            "collection_name": self._collection_name,
        }
        embedding = self._load_embedding_function()
        if embedding is not None:
            from_texts_kwargs["embedding"] = embedding

        try:
            self._vectorstore = Chroma.from_texts(**from_texts_kwargs)
        except ImportError:
            # Re-raise so connect() falls back to keyword mode.
            raise
        except Exception as exc:
            raise ConnectorError(f"Chroma index build failed: {exc!r}") from exc
        self._bm25_index = _build_bm25_index(texts, metadatas, ids)

    def _load_embedding_function(self) -> Any:
        """Build a LangChain ``Embeddings`` wrapper around the configured model.

        Returns ``None`` when no embedder is configured, when the
        ``sentence-transformers`` extra is missing, or when the model
        fails to load — Chroma then uses its default embedder so ingest
        is never blocked by a misconfigured custom model.
        """
        model_name = self._embedder_model_name
        if not model_name:
            return None
        try:
            from langchain_community.embeddings import (  # type: ignore[import-not-found,unused-ignore]
                HuggingFaceEmbeddings,
            )
        except ImportError:
            logger.info(
                "embedder_wrapper_unavailable",
                connector="DocumentStoreConnector",
                hint="Install machina-ai[docs-rag] for HuggingFace embedding wrappers",
                requested_model=model_name,
            )
            return None
        try:
            return HuggingFaceEmbeddings(model_name=model_name)
        except Exception as exc:
            logger.warning(
                "embedder_load_failed",
                connector="DocumentStoreConnector",
                requested_model=model_name,
                error=str(exc),
                hint="Falling back to Chroma's default embedder",
            )
            return None

    def _rag_search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Search using ChromaDB, optionally fused with BM25 via RRF.

        When the ``[docs-rag-hybrid]`` extra is installed and a BM25
        index was built at connect-time, this runs both retrievers in
        parallel and fuses their rankings with Reciprocal Rank Fusion
        before returning the top-K. Otherwise it falls back to pure
        dense retrieval.
        """
        if self._vectorstore is None:
            return []

        # ``top_k`` here is already the over-fetched budget computed by
        # search() (caller_top_k * _PARENT_OVERFETCH_FACTOR), which
        # gives RRF fusion and parent dedup enough headroom without an
        # additional inner multiplier.
        retrieve_k = top_k
        dense_pairs = self._dense_search(query, k=retrieve_k, filters=filters)
        if self._bm25_index is None:
            dense_chunks = [chunk for chunk, _ in dense_pairs]
            if self._reranker is not None:
                return self._apply_reranker(query, dense_chunks, top_k=top_k)
            return dense_chunks[:top_k]

        # Key chunks by chunk_id so RRF (which only sees ids) can be
        # joined back to the actual DocumentChunk objects.
        dense_ranking: list[tuple[str, float]] = [
            (chunk.chunk_id, float(score)) for chunk, score in dense_pairs if chunk.chunk_id
        ]
        chunks_by_id: dict[str, DocumentChunk] = {
            chunk.chunk_id: chunk for chunk, _ in dense_pairs if chunk.chunk_id
        }

        sparse_hits = self._bm25_index.search(query, k=retrieve_k, filters=filters)

        # Resurrect chunks that only the sparse retriever surfaced.
        for chunk_id, _ in sparse_hits:
            if chunk_id not in chunks_by_id:
                chunk = self._chunk_by_id.get(chunk_id)
                if chunk is not None:
                    chunks_by_id[chunk_id] = chunk

        from machina.connectors.docs.hybrid import rrf_fuse

        fused = rrf_fuse([dense_ranking, list(sparse_hits)])
        out: list[DocumentChunk] = []
        # When a reranker is configured, score the full fused candidate
        # set with the cross-encoder and let its order win; otherwise the
        # RRF order is returned as-is.
        if self._reranker is not None:
            fused_chunks: list[DocumentChunk] = []
            for chunk_id, fused_score in fused:
                chunk = chunks_by_id.get(chunk_id)
                if chunk is None:
                    continue
                chunk.score = float(fused_score)
                fused_chunks.append(chunk)
            return self._apply_reranker(query, fused_chunks, top_k=top_k)

        for chunk_id, fused_score in fused:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            chunk.score = float(fused_score)
            out.append(chunk)
            if len(out) >= top_k:
                break
        return out

    def _apply_reranker(
        self,
        query: str,
        candidates: list[DocumentChunk],
        *,
        top_k: int,
    ) -> list[DocumentChunk]:
        """Reorder ``candidates`` with the cross-encoder and return top-K.

        Candidates without a ``chunk_id`` are not handed to the reranker
        (the source:page fallback used to collide for chunks sharing a
        page when chunk_id was empty). They are appended after the
        reranked block in their original order.
        """
        if not candidates or self._reranker is None:
            return candidates[:top_k]

        rerankable: list[DocumentChunk] = [c for c in candidates if c.chunk_id]
        unrerankable: list[DocumentChunk] = [c for c in candidates if not c.chunk_id]
        if not rerankable:
            return candidates[:top_k]

        pairs = [(chunk.chunk_id, chunk.content) for chunk in rerankable]
        by_key = dict(zip([c.chunk_id for c in rerankable], rerankable, strict=True))
        try:
            scored = self._reranker.rerank(query, pairs)
        except Exception as exc:
            # Reranker raised mid-query (CUDA OOM, torch dynamic shape,
            # tokenizer error). Keep the RRF order rather than failing
            # the whole search and log so the failure is observable.
            logger.warning(
                "reranker_failed",
                connector="DocumentStoreConnector",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return candidates[:top_k]
        if scored is None:
            # Reranker unavailable or scoring failed — keep the upstream
            # RRF order and scores instead of overwriting with zeros.
            return candidates[:top_k]
        out: list[DocumentChunk] = []
        for key, score in scored:
            chunk = by_key.get(key)
            if chunk is None:
                continue
            chunk.score = float(score)
            out.append(chunk)
            if len(out) >= top_k:
                break
        # Append any chunks the reranker couldn't score (no chunk_id) in
        # their original RRF order so they remain reachable.
        for chunk in unrerankable:
            if len(out) >= top_k:
                break
            out.append(chunk)
        return out

    def _dense_search(
        self,
        query: str,
        *,
        k: int,
        filters: dict[str, Any] | None,
    ) -> list[tuple[DocumentChunk, float]]:
        """Run only the dense (Chroma) side and return ``(chunk, score)``."""
        where = _build_chroma_where(filters)
        kwargs: dict[str, Any] = {"k": k}
        if where is not None:
            kwargs["filter"] = where

        try:
            results = self._vectorstore.similarity_search_with_score(query, **kwargs)
        except (TypeError, ValueError) as exc:
            # Older Chroma signatures don't accept ``filter=`` (TypeError);
            # newer ones reject malformed where-clauses with ValueError /
            # InvalidArgumentError (a ValueError subclass). Both fall back
            # to post-filtering so a single bad filter shape doesn't kill
            # the whole search.
            logger.warning(
                "rag_filter_fallback",
                connector="DocumentStoreConnector",
                operation="similarity_search",
                error=str(exc),
            )
            try:
                results = self._vectorstore.similarity_search_with_score(query, k=k * 3)
            except Exception as inner_exc:
                raise ConnectorError(f"Vector store search failed: {inner_exc!r}") from inner_exc
            results = _post_filter_results(results, filters)
        except Exception as exc:
            # Embedding-time failures (HF OSError, CUDA OOM, torch type errors,
            # Chroma backend errors) bypass the filter-fallback path above.
            # Wrap them as ConnectorError so the agent's error policy can
            # apply retry / circuit-breaker semantics instead of crashing.
            logger.warning(
                "rag_search_failed",
                connector="DocumentStoreConnector",
                operation="similarity_search",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ConnectorError(f"Vector store search failed: {exc!r}") from exc

        return [
            (
                DocumentChunk(
                    content=doc.page_content,
                    source=doc.metadata.get("source", ""),
                    page=doc.metadata.get("page", 0),
                    score=float(score),
                    chunk_id=doc.metadata.get("chunk_id", ""),
                    parent_id=doc.metadata.get("parent_id", ""),
                    start_offset=int(doc.metadata.get("start_offset", 0) or 0),
                    is_table=bool(doc.metadata.get("is_table", False)),
                    asset_id=doc.metadata.get("asset_id", ""),
                    equipment_class_code=doc.metadata.get("equipment_class_code", ""),
                    doc_type=doc.metadata.get("doc_type", ""),
                    section_title=doc.metadata.get("section_title", ""),
                    metadata=doc.metadata,
                ),
                float(score),
            )
            for doc, score in results
        ]

    # ------------------------------------------------------------------
    # Keyword fallback
    # ------------------------------------------------------------------

    def _build_keyword_index(self, documents: list[dict[str, Any]]) -> None:
        """Build in-memory keyword index (no dependencies needed)."""
        # Shares the index-construction path with the RAG builder so
        # both modes produce identically shaped chunks (parent_id,
        # start_offset, section_title metadata). No vector store or
        # BM25 sidecar is built — _keyword_search reads self._chunks
        # directly.
        self._iter_doc_chunks(documents)

    def _keyword_search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Simple keyword matching as RAG fallback."""
        query_terms = set(query.lower().split())
        scored: list[tuple[float, DocumentChunk]] = []

        for chunk in self._chunks:
            if not _chunk_matches_filters(chunk, filters):
                continue
            content_lower = chunk.content.lower()
            matches = sum(1 for term in query_terms if term in content_lower)
            if matches > 0:
                score = matches / len(query_terms) if query_terms else 0.0
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = scored[:top_k]
        return [
            DocumentChunk(
                content=chunk.content,
                source=chunk.source,
                page=chunk.page,
                score=round(score, 3),
                chunk_id=chunk.chunk_id,
                parent_id=chunk.parent_id,
                start_offset=chunk.start_offset,
                is_table=chunk.is_table,
                asset_id=chunk.asset_id,
                equipment_class_code=chunk.equipment_class_code,
                doc_type=chunk.doc_type,
                section_title=chunk.section_title,
                metadata=chunk.metadata,
            )
            for score, chunk in results
        ]

    # ------------------------------------------------------------------
    # Document loading
    # ------------------------------------------------------------------

    def _load_documents(self) -> list[dict[str, Any]]:
        """Load documents from configured paths.

        Each loader returns one record per logical page. PDFs and DOCX
        files yield one record per source page so chunks carry an
        accurate ``page`` number; plain-text files yield a single
        page-0 record.
        """
        documents: list[dict[str, Any]] = []
        for path in self._paths:
            path = Path(path)
            if path.is_file():
                documents.extend(self._load_file(path))
            elif path.is_dir():
                for file_path in sorted(path.rglob("*")):
                    if file_path.is_file():
                        # Skip sidecar files — they are metadata, not content.
                        if file_path.name.endswith(".meta.yaml"):
                            continue
                        documents.extend(self._load_file(file_path))
        return documents

    def _load_file(self, file_path: Path) -> list[dict[str, Any]]:
        """Load a single file. Returns one record per page (or one for text)."""
        suffix = file_path.suffix.lower()
        doc_metadata = DocumentMetadata.from_path(file_path)

        if suffix in (".txt", ".md"):
            raw = file_path.read_text(encoding="utf-8", errors="replace")
            content = strip_frontmatter(raw)
            return [
                {
                    "content": content,
                    "source": str(file_path),
                    "page": 0,
                    "doc_metadata": doc_metadata,
                }
            ]

        if suffix == ".pdf":
            return [{**rec, "doc_metadata": doc_metadata} for rec in self._load_pdf(file_path)]

        if suffix in (".docx", ".doc"):
            return [{**rec, "doc_metadata": doc_metadata} for rec in self._load_docx(file_path)]

        return []

    def _load_pdf(self, file_path: Path) -> list[dict[str, Any]]:
        """Load a PDF. Try the layout-aware parser first, fall back to PyPDFLoader.

        When :class:`LayoutAwareParser` returns a structured document
        (Docling is installed and conversion succeeded), emit a single
        record carrying ``parsed`` so ``_iter_doc_chunks`` routes to
        ``split_structured`` and tables stay atomic. Otherwise fall
        back to the per-page PyPDFLoader path that preserves page
        numbers in flat-text mode.
        """
        parsed = self._parser.parse(file_path)
        if parsed is not None:
            return [
                {
                    "content": "",  # ignored when ``parsed`` is present
                    "source": str(file_path),
                    "page": 0,
                    "parsed": parsed,
                }
            ]
        try:
            from langchain_community.document_loaders import (  # type: ignore[import-not-found,unused-ignore]
                PyPDFLoader,
            )

            loader = PyPDFLoader(str(file_path))
            pages = loader.load()
            # PyPDFLoader sets metadata['page'] as a 0-based index;
            # surface it 1-based to match the user-visible page number.
            return [
                {
                    "content": page.page_content,
                    "source": str(file_path),
                    "page": int(page.metadata.get("page", i)) + 1,
                }
                for i, page in enumerate(pages)
            ]
        except ImportError:
            logger.warning(
                "pdf_loader_unavailable",
                connector="DocumentStoreConnector",
                file=str(file_path),
                hint="Install machina-ai[docs-rag] for PDF support",
            )
            return []
        except Exception as exc:
            logger.warning(
                "pdf_load_failed",
                connector="DocumentStoreConnector",
                file=str(file_path),
                error=str(exc),
            )
            return []

    def _load_docx(self, file_path: Path) -> list[dict[str, Any]]:
        """Load a DOCX file. Try layout-aware parser first, fall back to Docx2txtLoader.

        DOCX has no native page concept; the layout-aware path preserves
        heading levels so :meth:`SectionAwareSplitter.split_structured`
        produces well-bounded parents. The flat-text fallback emits one
        record with page=1.
        """
        parsed = self._parser.parse(file_path)
        if parsed is not None:
            return [
                {
                    "content": "",  # ignored when ``parsed`` is present
                    "source": str(file_path),
                    "page": 0,
                    "parsed": parsed,
                }
            ]
        try:
            from langchain_community.document_loaders import Docx2txtLoader

            loader = Docx2txtLoader(str(file_path))
            pages = loader.load()
            content = "\n\n".join(page.page_content for page in pages)
            return [
                {
                    "content": content,
                    "source": str(file_path),
                    "page": 1,
                }
            ]
        except ImportError:
            logger.warning(
                "docx_loader_unavailable",
                connector="DocumentStoreConnector",
                file=str(file_path),
                hint="Install machina-ai[docs-rag] for DOCX support",
            )
            return []
        except Exception as exc:
            logger.warning(
                "docx_load_failed",
                connector="DocumentStoreConnector",
                file=str(file_path),
                error=str(exc),
            )
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

    def _expand_to_parents(
        self, results: list[DocumentChunk], *, top_k: int
    ) -> list[DocumentChunk]:
        """Replace each match-chunk's ``content`` with its parent section.

        Runs after ranking so embedding / BM25 / rerank operate on small
        match-chunks but the LLM sees the full surrounding section.
        Match-chunks that don't carry a ``parent_id`` (or whose parent
        is no longer in the index — e.g. an orphan vector left in a
        persistent Chroma collection from a previous corpus) are
        returned unchanged. Oversized parents are windowed around the
        match using its char offset (no fragile substring search).

        Dedup-by-parent collapses adjacent winners that share a parent;
        the caller pre-fetched ``top_k * _PARENT_OVERFETCH_FACTOR`` so
        the final list still satisfies the caller's ``top_k`` after
        collapse.
        """
        if not self._parent_by_id:
            return results[:top_k]
        seen_parents: set[str] = set()
        expanded: list[DocumentChunk] = []
        for chunk in results:
            if len(expanded) >= top_k:
                break
            if not chunk.content.strip():
                # Skip whitespace-only matches — they confuse the
                # reranker and add no signal to the LLM.
                continue
            parent = self._parent_by_id.get(chunk.parent_id) if chunk.parent_id else None
            if parent is None:
                # Orphan match-chunk (no parent registered or persistent
                # store stale). Return the raw chunk so the caller still
                # gets a usable result.
                expanded.append(chunk)
                continue
            if parent.parent_id in seen_parents:
                continue
            seen_parents.add(parent.parent_id)
            text = self._splitter.window_parent(parent, _synthesize_match(chunk))
            if len(text) < len(parent.text):
                logger.warning(
                    "parent_section_windowed",
                    connector="DocumentStoreConnector",
                    source=chunk.source,
                    section_title=parent.title,
                    parent_chars=len(parent.text),
                    window_chars=len(text),
                )
            projected_meta = {
                **chunk.metadata,
                "section_title": parent.title or chunk.section_title,
                "parent_id": parent.parent_id,
            }
            expanded.append(
                DocumentChunk(
                    content=text,
                    source=chunk.source,
                    page=chunk.page,
                    score=chunk.score,
                    chunk_id=chunk.chunk_id,
                    parent_id=chunk.parent_id,
                    start_offset=chunk.start_offset,
                    is_table=chunk.is_table,
                    asset_id=chunk.asset_id,
                    equipment_class_code=chunk.equipment_class_code,
                    doc_type=chunk.doc_type,
                    section_title=parent.title or chunk.section_title,
                    metadata=projected_meta,
                )
            )
        return expanded

    @staticmethod
    def _merge_filters(filters: dict[str, Any] | None, asset_id: str) -> dict[str, Any] | None:
        """Combine the ``asset_id`` shortcut with an explicit ``filters`` dict."""
        if not asset_id and not filters:
            return None
        merged: dict[str, Any] = dict(filters or {})
        if asset_id:
            merged.setdefault("asset_id", asset_id)
        return merged or None


def _build_bm25_index(texts: list[str], metadatas: list[dict[str, Any]], ids: list[str]) -> Any:
    """Build a sparse BM25 index alongside the dense Chroma collection.

    Returns ``None`` when the ``[docs-rag-hybrid]`` extra is not
    installed; callers degrade to dense-only retrieval in that case.
    """
    try:
        from machina.connectors.docs.hybrid import BM25Index
    except ImportError:  # pragma: no cover — hybrid.py has no heavy imports at module level
        return None

    index = BM25Index()
    for chunk_id, text, meta in zip(ids, texts, metadatas, strict=True):
        index.add(chunk_id, text, meta)
    try:
        index.build()
    except ImportError:
        logger.info(
            "bm25_index_unavailable",
            connector="DocumentStoreConnector",
            hint="Install machina-ai[docs-rag-hybrid] for hybrid retrieval",
        )
        return None
    except Exception as exc:
        logger.warning(
            "bm25_index_build_failed",
            connector="DocumentStoreConnector",
            error=str(exc),
        )
        return None
    return index


def _make_chunk_id(*, source: str, page: int, section_title: str, index: int) -> str:
    """Deterministic chunk identifier (stable across runs).

    Fields are joined with NUL so ambiguous separators inside any field
    (a ``|`` in a section title, a path component containing the joiner)
    cannot produce colliding keys. ``usedforsecurity=False`` keeps the
    hash usable on FIPS-restricted interpreters where MD5 is rejected.
    """
    key = "\x00".join((source, section_title, str(page), str(index)))
    return hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest()


def _build_chroma_where(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate a flat filter dict into a Chroma ``where`` clause."""
    if not filters:
        return None
    # Chroma expects ``{"field": value}`` for equality and
    # ``{"$and": [...]}`` for multiple constraints.
    if len(filters) == 1:
        key, value = next(iter(filters.items()))
        return {key: value}
    return {"$and": [{key: value} for key, value in filters.items()]}


def _post_filter_results(
    results: list[tuple[Any, float]], filters: dict[str, Any] | None
) -> list[tuple[Any, float]]:
    """Fallback metadata filter when the vector store doesn't support ``where``."""
    if not filters:
        return results
    out: list[tuple[Any, float]] = []
    for doc, score in results:
        if _doc_metadata_matches(doc.metadata, filters):
            out.append((doc, score))
    return out


def _doc_metadata_matches(meta: dict[str, Any], filters: dict[str, Any]) -> bool:
    return all(meta.get(key) == value for key, value in filters.items())


def _synthesize_match(chunk: DocumentChunk) -> MatchChunk:
    """Build a transient MatchChunk so window_parent can position the window.

    The connector indexes the chunk's ``start_offset`` so the splitter
    only needs that plus the match text to anchor a window.
    """
    return MatchChunk(
        text=chunk.content,
        parent_id=chunk.parent_id,
        section_title=chunk.section_title,
        section_level=0,
        index_in_section=0,
        source=chunk.source,
        start_offset=chunk.start_offset,
    )


def _chunk_matches_filters(chunk: DocumentChunk, filters: dict[str, Any] | None) -> bool:
    """Whether ``chunk`` satisfies every key/value in ``filters``."""
    if not filters:
        return True
    for key, value in filters.items():
        if key == "asset_id":
            if chunk.asset_id != value:
                return False
        elif key == "equipment_class_code":
            if chunk.equipment_class_code != value:
                return False
        elif key == "doc_type":
            if chunk.doc_type != value:
                return False
        elif key == "section_title":
            if chunk.section_title != value:
                return False
        else:
            if chunk.metadata.get(key) != value:
                return False
    return True
