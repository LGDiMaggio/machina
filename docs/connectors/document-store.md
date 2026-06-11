# Document Store Connector

The `DocumentStoreConnector` ingests PDF, DOCX, Markdown, and text manuals and exposes them to the agent via semantic search. It is designed for technical documentation — equipment manuals, troubleshooting guides, work procedures, datasheets — where a single chunk is often less useful than the surrounding section.

The connector is layered so each capability is opt-in. The base path (keyword fallback) has no heavy dependencies; layout-aware parsing, hybrid retrieval, and cross-encoder reranking are pulled in via extras when the use-case justifies the install cost.

| Capability | Extra | Default | Use when |
|---|---|---|---|
| Keyword fallback | none | ✅ | smoke tests, quickstart |
| Dense vector search | `[docs-rag]` | ✅ when installed | semantic retrieval |
| Hybrid BM25 + dense (RRF) | `[docs-rag-hybrid]` | ✅ when installed | technical IDs (`SKF 6310-2RS`), part numbers, acronyms |
| Cross-encoder reranker | `[docs-rag-rerank]` | opt-in (`reranker_model=`) | precision-critical retrieval |
| Layout-aware parsing (Docling) | `[docs-rag-parsing]` | ✅ when installed | PDFs with tables, multi-column layouts, figure captions |

Install everything with `pip install machina-ai[docs-rag-pro]`.

## Quickstart

```python
from machina.connectors.docs import DocumentStoreConnector

docs = DocumentStoreConnector(paths=["manuals/", "procedures/"])
await docs.connect()
results = await docs.search("bearing replacement", asset_id="P-201")
for chunk in results:
    print(f"[{chunk.source} p.{chunk.page}] § {chunk.section_title}")
    print(chunk.content[:200])
```

When `langchain-chroma` + `chromadb` are not installed the connector falls back to an in-memory keyword search (logging a WARNING that names the missing package) so the quickstart works without a heavy install.

## Metadata schema

Every chunk carries a small, structured metadata record that powers pre-retrieval filtering. Metadata can come from three sources, applied in order:

1. **Sidecar file** — `manual.pdf` next to `manual.pdf.meta.yaml`
2. **YAML frontmatter** — at the top of `.md` / `.txt` files
3. **Path inference** — best-effort guess from filename / parent directory

```yaml
# manual.pdf.meta.yaml
asset_id: P-201
equipment_class_code: PU      # ISO 14224 Annex A
doc_type: manual              # manual | procedure | datasheet | troubleshooting | other
section_title: Bearing Replacement  # optional default
```

Frontmatter format inside Markdown / text:

```markdown
---
asset_id: P-201
doc_type: procedure
---
# Bearing Replacement Procedure
...
```

Filter by any indexed field at query time:

```python
await docs.search(
    "torque",
    filters={"asset_id": "P-201", "doc_type": "procedure"},
)
```

`asset_id=` is a shortcut for `filters={"asset_id": ...}`.

## Section-aware chunking

The splitter detects sections from three signals and keeps each section's body as a single **parent**, while indexing smaller **match chunks** for embedding / BM25 / rerank.

* **Markdown** — ATX headings (`#`, `##`, ...), fence-aware so code samples don't spawn phantom sections
* **Numbered headings** — `1. Introduction`, `2.1 Bearing Replacement` — only when set off by a blank line, to avoid mistaking body list items for headings
* **ALL-CAPS headings** — `BEARING REPLACEMENT PROCEDURE`, also with blank-line context

At query time, the retrieved match chunk is replaced by its parent section before being handed to the LLM, so a query for "Step 3" returns the entire multi-step procedure, not a partial chunk. Sections that exceed `max_parent_chars` (default 8000) are windowed around the matched offset.

## Layout-aware parsing

Install `machina-ai[docs-rag-parsing]` to enable Docling-based parsing. PDFs and DOCX files are parsed into a structured `ParsedDocument` with:

* **Sections** — heading title, level, body text, page range
* **Tables** — rendered as Markdown and indexed as one atomic chunk that retrieval can never split mid-row

Layout-aware parsing is best-effort: if Docling isn't installed, or if it raises on a specific file, the connector logs a warning and falls back to `PyPDFLoader` / `Docx2txtLoader`. A single bad PDF never blocks the rest of a corpus.

## Hybrid retrieval

With `[docs-rag-hybrid]` installed, every dense Chroma query is paired with a BM25 query over the same chunk corpus and the two rankings are fused with [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) (k=60). Hybrid retrieval recovers technical identifiers — `SKF 6310-2RS`, `WO-2026-0087`, ISO codes — that dense embeddings often miss while keeping semantic recall for prose queries.

## Cross-encoder reranking

Pass `reranker_model=` to wrap the fused candidate set in a cross-encoder for a final reordering pass:

```python
DocumentStoreConnector(
    paths=["manuals/"],
    reranker_model="BAAI/bge-reranker-base",
)
```

Requires `[docs-rag-rerank]`. The model is loaded lazily on first query so connect time stays fast; if loading fails the connector keeps the RRF order rather than returning an empty result.

## Swappable embedder

Pass `embedder=` to use a custom `sentence-transformers` model for the dense index instead of Chroma's default:

```python
DocumentStoreConnector(
    paths=["manuals/"],
    embedder="BAAI/bge-m3",
)
```

`bge-m3` works well for multilingual technical content (Italian, English, German manuals in the same corpus). Requires `[docs-rag-rerank]` for the `sentence-transformers` runtime. If the model fails to load — extra missing, model not downloaded, GPU / CPU mismatch — the connector falls back to Chroma's default embedder so ingest is never blocked.

## Citation contract

Search results carry stable identifiers the agent can cite:

```python
@dataclass
class DocumentChunk:
    content: str
    source: str            # file path
    page: int              # 1-based, or 0 if unknown
    chunk_id: str          # deterministic across runs
    parent_id: str         # join key for the parent section
    section_title: str     # detected by splitter or sidecar
    asset_id: str
    equipment_class_code: str
    doc_type: str
    score: float
```

The agent runtime registers retrieved `chunk_id`s per turn so the LLM can reference them in its answer; the parser validates each citation against the registry to reject hallucinated chunk ids.

## Failure modes

| Failure | Behavior |
|---|---|
| `langchain-chroma` / `chromadb` not installed | Falls back to in-memory keyword search with a WARNING (names the `pip install -U "machina-ai[docs-rag]"` remedy on legacy installs) |
| `rank_bm25` not installed | Dense-only retrieval |
| Reranker model fails to load | Returns RRF order (no rerank) |
| Layout parser fails on a file | Logs warning, falls back to `PyPDFLoader` for that file |
| Embedder model fails to load | Falls back to Chroma's default embedder |
| Sidecar YAML missing or malformed | Logs warning, indexes with empty metadata |
| File extension unsupported | Skipped silently |

## Configuration reference

| Param | Default | Notes |
|---|---|---|
| `paths` | `[]` | Files or directories. Directories are walked recursively. |
| `collection_name` | `"machina_docs"` | ChromaDB collection name |
| `chunk_size` | `1000` | Target match-chunk size in characters |
| `chunk_overlap` | `200` | Overlap between consecutive match chunks |
| `reranker_model` | `None` | `sentence-transformers` cross-encoder name |
| `embedder` | `None` | `sentence-transformers` embedding model name |

## See also

* [Citation contract](../architecture.md) — how the agent surfaces source references
* [Domain model](../domain.md) — `Asset`, `equipment_class_code`, `doc_type` taxonomies
