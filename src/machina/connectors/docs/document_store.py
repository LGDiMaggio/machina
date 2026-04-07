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
        metadata: Additional metadata from the document loader.
    """

    content: str
    source: str = ""
    page: int = 0
    score: float = 0.0
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

    Args:
        paths: List of directories or files to ingest.
        collection_name: Name for the ChromaDB collection.
        chunk_size: Target size for text chunks (in characters).
        chunk_overlap: Overlap between consecutive chunks.

    Example:
        ```python
        docs = DocumentStoreConnector(paths=["manuals/", "procedures/"])
        await docs.connect()
        results = await docs.search("pump P-201 bearing replacement")
        for chunk in results:
            print(f"[{chunk.source} p.{chunk.page}] {chunk.content[:100]}")
        ```
    """

    capabilities: ClassVar[list[str]] = ["search_documents", "retrieve_section"]

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
    ) -> list[DocumentChunk]:
        """Search documents for passages relevant to the query.

        Args:
            query: The search query.
            top_k: Maximum number of results to return.
            asset_id: Optional asset ID to scope the search.

        Returns:
            List of relevant document chunks, ranked by relevance.
        """
        self._ensure_connected()
        if self._use_rag:
            return self._rag_search(query, top_k=top_k, asset_id=asset_id)
        return self._keyword_search(query, top_k=top_k, asset_id=asset_id)

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
        from langchain.text_splitter import (  # type: ignore[import-not-found]
            RecursiveCharacterTextSplitter,
        )
        from langchain_community.vectorstores import Chroma  # type: ignore[import-not-found]

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        )

        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []
        ids: list[str] = []

        for doc in documents:
            chunks = splitter.split_text(doc["content"])
            for i, chunk_text in enumerate(chunks):
                chunk_id = hashlib.md5(
                    f"{doc['source']}:{doc.get('page', 0)}:{i}".encode()
                ).hexdigest()
                texts.append(chunk_text)
                meta = {
                    "source": doc["source"],
                    "page": doc.get("page", 0),
                }
                metadatas.append(meta)
                ids.append(chunk_id)

                self._chunks.append(
                    DocumentChunk(
                        content=chunk_text,
                        source=doc["source"],
                        page=doc.get("page", 0),
                        metadata=meta,
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
        asset_id: str = "",
    ) -> list[DocumentChunk]:
        """Search using the ChromaDB vector store."""
        if self._vectorstore is None:
            return []

        where_filter: dict[str, Any] | None = None
        if asset_id:
            where_filter = {"source": {"$contains": asset_id}}

        results = self._vectorstore.similarity_search_with_score(
            query,
            k=top_k,
            filter=where_filter,
        )

        return [
            DocumentChunk(
                content=doc.page_content,
                source=doc.metadata.get("source", ""),
                page=doc.metadata.get("page", 0),
                score=float(score),
                metadata=doc.metadata,
            )
            for doc, score in results
        ]

    # ------------------------------------------------------------------
    # Keyword fallback
    # ------------------------------------------------------------------

    def _build_keyword_index(self, documents: list[dict[str, Any]]) -> None:
        """Build in-memory keyword index (no dependencies needed)."""
        for doc in documents:
            content = doc["content"]
            # Simple chunking by paragraphs or fixed size
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            for i, para in enumerate(paragraphs):
                self._chunks.append(
                    DocumentChunk(
                        content=para,
                        source=doc["source"],
                        page=doc.get("page", 0) or i + 1,
                        metadata={"source": doc["source"]},
                    )
                )

    def _keyword_search(
        self,
        query: str,
        *,
        top_k: int = 5,
        asset_id: str = "",
    ) -> list[DocumentChunk]:
        """Simple keyword matching as RAG fallback."""
        query_terms = set(query.lower().split())
        scored: list[tuple[float, DocumentChunk]] = []

        for chunk in self._chunks:
            if asset_id and asset_id.lower() not in chunk.content.lower():
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
                        doc = self._load_file(file_path)
                        if doc:
                            documents.append(doc)
        return documents

    def _load_file(self, file_path: Path) -> dict[str, Any] | None:
        """Load a single file. Supports .txt, .md, .pdf (via LangChain)."""
        suffix = file_path.suffix.lower()

        if suffix in (".txt", ".md"):
            content = file_path.read_text(encoding="utf-8", errors="replace")
            return {"content": content, "source": str(file_path)}

        if suffix == ".pdf":
            return self._load_pdf(file_path)

        if suffix in (".docx", ".doc"):
            return self._load_docx(file_path)

        # Unsupported file type — skip
        return None

    def _load_pdf(self, file_path: Path) -> dict[str, Any] | None:
        """Load a PDF file using LangChain's PDF loader, or skip if unavailable."""
        try:
            from langchain_community.document_loaders import (  # type: ignore[import-not-found]
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectorError("Not connected — call connect() first")
