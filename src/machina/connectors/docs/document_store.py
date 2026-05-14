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
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability
from machina.connectors.docs.chunking import (
    MatchChunk,
    ParentSection,
    SectionAwareSplitter,
)
from machina.connectors.docs.metadata import DocumentMetadata, strip_frontmatter
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


# Over-fetch multiplier so parent dedup in _expand_to_parents can still
# honour the caller's top_k after collapsing matches that share a parent.
_PARENT_OVERFETCH_FACTOR = 6


@dataclass
class DocumentChunk:
    """A retrieved passage from a document.

    Args:
        content: The text content of the passage.
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
    parent_id: str = ""
    start_offset: int = 0
    asset_id: str = ""
    equipment_class_code: str = ""
    doc_type: str = ""
    section_title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

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
        collection_name: Name for the ChromaDB collection.
        chunk_size: Target size for text chunks (in characters).
        chunk_overlap: Overlap between consecutive chunks.

    Example:
        ```python
        docs = DocumentStoreConnector(paths=["manuals/", "procedures/"])
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
    ) -> None:
        self._paths = [Path(p) for p in (paths or [])]
        self._collection_name = collection_name
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._reranker_model_name = reranker_model
        self._connected = False
        self._use_rag = False

        # Splitter is retained so _expand_to_parents can call
        # window_parent for oversized-section truncation using the
        # splitter's max_parent_chars / parent_window contract.
        self._splitter = SectionAwareSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

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
        documents = await asyncio.to_thread(self._load_documents)
        if not documents:
            logger.warning(
                "no_documents_found",
                connector="DocumentStoreConnector",
                paths=[str(p) for p in self._paths],
            )

        try:
            self._build_rag_index(documents)
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
            self._build_keyword_index(documents)
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
        # that share a parent_id.
        over_fetch = top_k * _PARENT_OVERFETCH_FACTOR
        if self._use_rag:
            results = await asyncio.to_thread(
                self._rag_search, query, top_k=over_fetch, filters=effective
            )
        else:
            results = self._keyword_search(query, top_k=over_fetch, filters=effective)
        return self._expand_to_parents(results, top_k=top_k)

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

    def _iter_doc_chunks(
        self, documents: list[dict[str, Any]]
    ) -> list[tuple[str, str, dict[str, Any], DocumentChunk]]:
        """Run the splitter over every document and produce indexable chunks.

        Returns a list of ``(chunk_id, text, chroma_meta, DocumentChunk)``
        tuples and populates ``self._parent_by_id`` / ``self._chunks`` /
        ``self._chunk_by_id``. Shared by the RAG and keyword index
        builders so both modes index against the same chunk shape.
        """
        out: list[tuple[str, str, dict[str, Any], DocumentChunk]] = []
        for doc in documents:
            doc_meta: DocumentMetadata = doc.get("doc_metadata") or DocumentMetadata()
            parents, matches = self._splitter.split(doc["content"], source=doc["source"])
            for parent in parents:
                self._parent_by_id[parent.parent_id] = parent
            base_page = doc.get("page", 0)
            for i, match in enumerate(matches):
                section_title = match.section_title or doc_meta.section_title
                page = base_page or 0
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
                    asset_id=doc_meta.asset_id,
                    equipment_class_code=doc_meta.equipment_class_code,
                    doc_type=doc_meta.doc_type,
                    section_title=section_title,
                    metadata=chroma_meta,
                )
                self._chunks.append(new_chunk)
                self._chunk_by_id[chunk_id] = new_chunk
                out.append((chunk_id, match.text, chroma_meta, new_chunk))
        return out

    def _build_rag_index(self, documents: list[dict[str, Any]]) -> None:
        """Build a ChromaDB vector store from parsed documents."""
        from langchain_community.vectorstores import (  # type: ignore[import-not-found,unused-ignore]
            Chroma,
        )

        indexed = self._iter_doc_chunks(documents)
        if not indexed:
            return
        ids = [chunk_id for chunk_id, _, _, _ in indexed]
        texts = [text for _, text, _, _ in indexed]
        metadatas = [meta for _, _, meta, _ in indexed]

        self._vectorstore = Chroma.from_texts(
            texts=texts,
            metadatas=metadatas,
            ids=ids,
            collection_name=self._collection_name,
        )
        self._bm25_index = _build_bm25_index(texts, metadatas, ids)

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
        scored = self._reranker.rerank(query, pairs)
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
            results = self._vectorstore.similarity_search_with_score(query, k=k * 3)
            results = _post_filter_results(results, filters)

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
        """Load a PDF file using LangChain's PDF loader, one record per page."""
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
        """Load a DOCX file using LangChain's DOCX loader.

        Docx2txtLoader returns a single Document covering the whole
        file (DOCX has no native page concept), so we always emit one
        record with page=1.
        """
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
