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
from machina.connectors.docs.metadata import DocumentMetadata, strip_frontmatter
from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


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
    ) -> None:
        self._paths = [Path(p) for p in (paths or [])]
        self._collection_name = collection_name
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._connected = False
        self._use_rag = False

        # Populated after connect()
        self._chunks: list[DocumentChunk] = []
        self._vectorstore: Any = None

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
            logger.info(
                "connected",
                connector="DocumentStoreConnector",
                mode="rag",
                document_count=len(documents),
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
        """Release resources."""
        self._vectorstore = None
        self._chunks.clear()
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
        if self._use_rag:
            return await asyncio.to_thread(self._rag_search, query, top_k=top_k, filters=effective)
        return self._keyword_search(query, top_k=top_k, filters=effective)

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

    def _build_rag_index(self, documents: list[dict[str, Any]]) -> None:
        """Build a ChromaDB vector store from parsed documents."""
        from langchain.text_splitter import (  # type: ignore[import-not-found,unused-ignore]
            RecursiveCharacterTextSplitter,
        )
        from langchain_community.vectorstores import (  # type: ignore[import-not-found,unused-ignore]
            Chroma,
        )

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        )

        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []
        ids: list[str] = []

        for doc in documents:
            doc_meta: DocumentMetadata = doc.get("doc_metadata") or DocumentMetadata()
            chunks = splitter.split_text(doc["content"])
            for i, chunk_text in enumerate(chunks):
                chunk_id = _make_chunk_id(
                    source=doc["source"],
                    page=doc.get("page", 0),
                    section_title=doc_meta.section_title,
                    index=i,
                )
                chroma_meta: dict[str, Any] = {
                    "source": doc["source"],
                    "page": doc.get("page", 0),
                    "chunk_id": chunk_id,
                }
                chroma_meta.update(doc_meta.to_chroma_dict())

                texts.append(chunk_text)
                metadatas.append(chroma_meta)
                ids.append(chunk_id)

                self._chunks.append(
                    DocumentChunk(
                        content=chunk_text,
                        source=doc["source"],
                        page=doc.get("page", 0),
                        chunk_id=chunk_id,
                        asset_id=doc_meta.asset_id,
                        equipment_class_code=doc_meta.equipment_class_code,
                        doc_type=doc_meta.doc_type,
                        section_title=doc_meta.section_title,
                        metadata=chroma_meta,
                    )
                )

        if texts:
            self._vectorstore = Chroma.from_texts(
                texts=texts,
                metadatas=metadatas,
                ids=ids,
                collection_name=self._collection_name,
            )

    def _rag_search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Search using the ChromaDB vector store."""
        if self._vectorstore is None:
            return []

        where = _build_chroma_where(filters)
        kwargs: dict[str, Any] = {"k": top_k}
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
            results = self._vectorstore.similarity_search_with_score(query, k=top_k * 3)
            results = _post_filter_results(results, filters)

        chunks = [
            DocumentChunk(
                content=doc.page_content,
                source=doc.metadata.get("source", ""),
                page=doc.metadata.get("page", 0),
                score=float(score),
                chunk_id=doc.metadata.get("chunk_id", ""),
                asset_id=doc.metadata.get("asset_id", ""),
                equipment_class_code=doc.metadata.get("equipment_class_code", ""),
                doc_type=doc.metadata.get("doc_type", ""),
                section_title=doc.metadata.get("section_title", ""),
                metadata=doc.metadata,
            )
            for doc, score in results
        ]

        return chunks[:top_k]

    # ------------------------------------------------------------------
    # Keyword fallback
    # ------------------------------------------------------------------

    def _build_keyword_index(self, documents: list[dict[str, Any]]) -> None:
        """Build in-memory keyword index (no dependencies needed)."""
        for doc in documents:
            doc_meta: DocumentMetadata = doc.get("doc_metadata") or DocumentMetadata()
            content = doc["content"]
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            for i, para in enumerate(paragraphs):
                chunk_id = _make_chunk_id(
                    source=doc["source"],
                    page=doc.get("page", 0) or i + 1,
                    section_title=doc_meta.section_title,
                    index=i,
                )
                self._chunks.append(
                    DocumentChunk(
                        content=para,
                        source=doc["source"],
                        page=doc.get("page", 0) or i + 1,
                        chunk_id=chunk_id,
                        asset_id=doc_meta.asset_id,
                        equipment_class_code=doc_meta.equipment_class_code,
                        doc_type=doc_meta.doc_type,
                        section_title=doc_meta.section_title,
                        metadata={"source": doc["source"], **doc_meta.to_chroma_dict()},
                    )
                )

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
        """Load documents from configured paths."""
        documents: list[dict[str, Any]] = []
        for path in self._paths:
            path = Path(path)
            if path.is_file():
                doc = self._load_file(path)
                if doc:
                    documents.append(doc)
            elif path.is_dir():
                for file_path in sorted(path.rglob("*")):
                    if file_path.is_file():
                        # Skip sidecar files — they are metadata, not content.
                        if file_path.name.endswith(".meta.yaml"):
                            continue
                        doc = self._load_file(file_path)
                        if doc:
                            documents.append(doc)
        return documents

    def _load_file(self, file_path: Path) -> dict[str, Any] | None:
        """Load a single file. Supports .txt, .md, .pdf (via LangChain)."""
        suffix = file_path.suffix.lower()
        doc_metadata = DocumentMetadata.from_path(file_path)

        if suffix in (".txt", ".md"):
            raw = file_path.read_text(encoding="utf-8", errors="replace")
            content = strip_frontmatter(raw)
            return {
                "content": content,
                "source": str(file_path),
                "doc_metadata": doc_metadata,
            }

        if suffix == ".pdf":
            loaded = self._load_pdf(file_path)
            if loaded is not None:
                loaded["doc_metadata"] = doc_metadata
            return loaded

        if suffix in (".docx", ".doc"):
            loaded = self._load_docx(file_path)
            if loaded is not None:
                loaded["doc_metadata"] = doc_metadata
            return loaded

        # Unsupported file type — skip
        return None

    def _load_pdf(self, file_path: Path) -> dict[str, Any] | None:
        """Load a PDF file using LangChain's PDF loader, or skip if unavailable."""
        try:
            from langchain_community.document_loaders import (  # type: ignore[import-not-found,unused-ignore]
                PyPDFLoader,
            )

            loader = PyPDFLoader(str(file_path))
            pages = loader.load()
            content = "\n\n".join(page.page_content for page in pages)
            return {"content": content, "source": str(file_path)}
        except ImportError:
            logger.warning(
                "pdf_loader_unavailable",
                connector="DocumentStoreConnector",
                file=str(file_path),
                hint="Install machina-ai[docs-rag] for PDF support",
            )
            return None
        except Exception as exc:
            logger.warning(
                "pdf_load_failed",
                connector="DocumentStoreConnector",
                file=str(file_path),
                error=str(exc),
            )
            return None

    def _load_docx(self, file_path: Path) -> dict[str, Any] | None:
        """Load a DOCX file using LangChain's DOCX loader, or skip."""
        try:
            from langchain_community.document_loaders import Docx2txtLoader

            loader = Docx2txtLoader(str(file_path))
            pages = loader.load()
            content = "\n\n".join(page.page_content for page in pages)
            return {"content": content, "source": str(file_path)}
        except ImportError:
            logger.warning(
                "docx_loader_unavailable",
                connector="DocumentStoreConnector",
                file=str(file_path),
                hint="Install machina-ai[docs-rag] for DOCX support",
            )
            return None
        except Exception as exc:
            logger.warning(
                "docx_load_failed",
                connector="DocumentStoreConnector",
                file=str(file_path),
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")

    @staticmethod
    def _merge_filters(filters: dict[str, Any] | None, asset_id: str) -> dict[str, Any] | None:
        """Combine the ``asset_id`` shortcut with an explicit ``filters`` dict."""
        if not asset_id and not filters:
            return None
        merged: dict[str, Any] = dict(filters or {})
        if asset_id:
            merged.setdefault("asset_id", asset_id)
        return merged or None


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
