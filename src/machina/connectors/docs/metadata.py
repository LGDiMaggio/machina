"""Structured metadata for document chunks.

Each ingested document can be tagged with structured metadata (asset_id,
equipment_class_code, doc_type, section_title) so the connector can filter
the search space *before* retrieval rather than post-filtering results.

Metadata is loaded from, in priority order:

1. A sidecar file ``<path>.meta.yaml`` next to the source document.
2. YAML frontmatter at the top of ``.md`` / ``.txt`` files (delimited by
   ``---`` on its own line).
3. Best-effort inference from the filename / parent directory.

All fields are optional. A missing sidecar is not an error; the chunk is
simply indexed without that metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)


_KNOWN_DOC_TYPES = frozenset({"manual", "procedure", "datasheet", "troubleshooting", "other"})

# Maximum length of a free-form metadata value when carried to the vector
# store. Long strings are truncated; non-scalar values are dropped entirely.
_MAX_METADATA_VALUE_LEN = 256

# Reserved metadata keys — extra keys that collide with these are dropped
# so user-authored frontmatter cannot overwrite system fields.
_RESERVED_METADATA_KEYS = frozenset({"source", "page", "chunk_id", "content", "score"})

# Asset IDs in PMI manuals typically look like P-201, COMP-301, M-12, V-007A.
# We match an uppercase prefix (1-6 chars), a hyphen, and a digit-led suffix.
_ASSET_ID_PATTERN = re.compile(r"\b([A-Z]{1,6})-(\d{1,5}[A-Z]?)(?![A-Za-z0-9])")

_DOC_TYPE_KEYWORDS: dict[str, str] = {
    "manual": "manual",
    "manuale": "manual",
    "procedure": "procedure",
    "procedura": "procedure",
    "datasheet": "datasheet",
    "scheda": "datasheet",
    "troubleshooting": "troubleshooting",
    "guasti": "troubleshooting",
}


@dataclass(frozen=True)
class DocumentMetadata:
    """Structured metadata for a document or chunk.

    All fields are optional. Empty strings mean "unknown" — they do not
    cause Chroma filters to reject the chunk.

    Args:
        asset_id: Domain asset identifier (e.g. ``"P-201"``).
        equipment_class_code: ISO 14224 Annex A Table A.4 code (e.g. ``"PU"``,
            ``"CO"``). Internal use only — not surfaced in product positioning.
        doc_type: One of ``manual``, ``procedure``, ``datasheet``,
            ``troubleshooting``, ``other``.
        section_title: Title of the section this chunk belongs to. Populated
            by section-aware chunking; empty by default.
        extra: Any additional key/value pairs from the sidecar/frontmatter.
    """

    asset_id: str = ""
    equipment_class_code: str = ""
    doc_type: str = ""
    section_title: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_chroma_dict(self) -> dict[str, Any]:
        """Return a flat ``dict`` suitable for Chroma metadata fields.

        Chroma's ``where=`` clause cannot match on empty strings reliably,
        so empty fields are omitted from the output.

        Free-form ``extra`` keys from sidecars / frontmatter are accepted
        as filterable metadata but are sanitized first: non-scalar values
        (lists, dicts, None) are dropped, strings are stripped of control
        characters and capped at ``_MAX_METADATA_VALUE_LEN``, and keys
        that collide with reserved system fields are rejected. This keeps
        a typo or hostile sidecar from injecting newlines / overlong
        payloads / system-field overrides into downstream consumers (the
        vector store, prompt context, or operator-facing logs).
        """
        out: dict[str, Any] = {}
        if self.asset_id:
            out["asset_id"] = self.asset_id
        if self.equipment_class_code:
            out["equipment_class_code"] = self.equipment_class_code
        if self.doc_type:
            out["doc_type"] = self.doc_type
        if self.section_title:
            out["section_title"] = self.section_title
        for key, value in self.extra.items():
            if key in _RESERVED_METADATA_KEYS or key in out:
                continue
            sanitized = _sanitize_metadata_value(value)
            if sanitized is None:
                continue
            out[key] = sanitized
        return out

    def merge(self, override: DocumentMetadata) -> DocumentMetadata:
        """Return a new metadata where non-empty fields in ``override`` win."""
        return DocumentMetadata(
            asset_id=override.asset_id or self.asset_id,
            equipment_class_code=override.equipment_class_code or self.equipment_class_code,
            doc_type=override.doc_type or self.doc_type,
            section_title=override.section_title or self.section_title,
            extra={**self.extra, **override.extra},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentMetadata:
        """Build a ``DocumentMetadata`` from a parsed YAML dict."""
        known = {"asset_id", "equipment_class_code", "doc_type", "section_title"}
        extra = {k: v for k, v in data.items() if k not in known}
        doc_type = str(data.get("doc_type", "")).strip().lower()
        if doc_type and doc_type not in _KNOWN_DOC_TYPES:
            logger.warning(
                "unknown_doc_type",
                operation="load_metadata",
                doc_type=doc_type,
                known=sorted(_KNOWN_DOC_TYPES),
            )
        return cls(
            asset_id=str(data.get("asset_id", "")).strip(),
            equipment_class_code=str(data.get("equipment_class_code", "")).strip(),
            doc_type=doc_type,
            section_title=str(data.get("section_title", "")).strip(),
            extra=extra,
        )

    @classmethod
    def from_path(cls, path: Path) -> DocumentMetadata:
        """Load metadata for ``path``, combining sidecar, frontmatter, and inference."""
        inferred = _infer_from_filename(path)
        sidecar = _load_sidecar(path)
        frontmatter = _load_frontmatter(path)
        # Priority: sidecar > frontmatter > inferred
        return inferred.merge(frontmatter).merge(sidecar)


def _load_sidecar(path: Path) -> DocumentMetadata:
    """Load metadata from ``<path>.meta.yaml`` if present."""
    sidecar = path.with_suffix(path.suffix + ".meta.yaml")
    if not sidecar.is_file():
        return DocumentMetadata()
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "sidecar_parse_failed",
            operation="load_metadata",
            sidecar=str(sidecar),
            error=str(exc),
        )
        return DocumentMetadata()
    if not isinstance(data, dict):
        logger.warning(
            "sidecar_not_a_mapping",
            operation="load_metadata",
            sidecar=str(sidecar),
        )
        return DocumentMetadata()
    return DocumentMetadata.from_dict(data)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _load_frontmatter(path: Path) -> DocumentMetadata:
    """Load YAML frontmatter from text/markdown files."""
    if path.suffix.lower() not in {".md", ".txt"}:
        return DocumentMetadata()
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return DocumentMetadata()
    match = _FRONTMATTER_RE.match(head)
    if not match:
        return DocumentMetadata()
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "frontmatter_parse_failed",
            operation="load_metadata",
            path=str(path),
            error=str(exc),
        )
        return DocumentMetadata()
    if not isinstance(data, dict):
        return DocumentMetadata()
    return DocumentMetadata.from_dict(data)


def strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block from ``text`` if present."""
    match = _FRONTMATTER_RE.match(text)
    return text[match.end() :] if match else text


def _infer_from_filename(path: Path) -> DocumentMetadata:
    """Best-effort metadata inference from filename and parent directory."""
    haystack = f"{path.parent.name}/{path.stem}"
    asset_id = ""
    match = _ASSET_ID_PATTERN.search(haystack)
    if match:
        asset_id = match.group(0)

    doc_type = ""
    haystack_lower = haystack.lower()
    for keyword, dtype in _DOC_TYPE_KEYWORDS.items():
        if keyword in haystack_lower:
            doc_type = dtype
            break

    return DocumentMetadata(asset_id=asset_id, doc_type=doc_type)


def _sanitize_metadata_value(value: Any) -> str | int | float | bool | None:
    """Return a vector-store-safe scalar value, or ``None`` to drop the entry.

    Strings are stripped of control characters and capped in length so a
    typo or malicious sidecar can't smuggle newlines / overlong payloads
    into downstream prompt context, log lines, or filter clauses.
    Integers, finite floats, and booleans pass through unchanged.
    ``NaN`` / ``+inf`` / ``-inf`` are dropped because Chroma where-clause
    handling for non-finite floats is inconsistent across versions and
    silently corrupts filter behavior. Anything else (lists, dicts,
    ``None``) is dropped.
    """
    import math

    # bool is a subclass of int — match it first to keep True/False as-is.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, str):
        cleaned = "".join(ch for ch in value if ch == " " or ch.isprintable())
        cleaned = cleaned.strip()
        if not cleaned:
            return None
        return cleaned[:_MAX_METADATA_VALUE_LEN]
    return None
