"""Tests for DocumentMetadata loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

from machina.connectors.docs.metadata import (
    DocumentMetadata,
    strip_frontmatter,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestDocumentMetadata:
    """Constructor, merge, and serialization."""

    def test_defaults(self) -> None:
        meta = DocumentMetadata()
        assert meta.asset_id == ""
        assert meta.doc_type == ""
        assert meta.to_chroma_dict() == {}

    def test_to_chroma_dict_omits_empty_fields(self) -> None:
        meta = DocumentMetadata(asset_id="P-201", doc_type="manual")
        result = meta.to_chroma_dict()
        assert result == {"asset_id": "P-201", "doc_type": "manual"}

    def test_merge_override_wins_for_non_empty(self) -> None:
        base = DocumentMetadata(asset_id="P-201", doc_type="manual")
        override = DocumentMetadata(doc_type="procedure")
        merged = base.merge(override)
        assert merged.asset_id == "P-201"
        assert merged.doc_type == "procedure"

    def test_from_dict_normalizes_doc_type(self) -> None:
        meta = DocumentMetadata.from_dict({"doc_type": "  Manual  "})
        assert meta.doc_type == "manual"

    def test_from_dict_preserves_extra_keys(self) -> None:
        meta = DocumentMetadata.from_dict({"asset_id": "P-201", "vendor": "SKF"})
        assert meta.extra == {"vendor": "SKF"}
        assert meta.to_chroma_dict()["vendor"] == "SKF"

    def test_to_chroma_dict_drops_reserved_extra_keys(self) -> None:
        # ``source`` and ``page`` are populated by the loader; user-authored
        # frontmatter must not be able to overwrite them.
        meta = DocumentMetadata(
            asset_id="P-201",
            extra={"source": "hijacked.pdf", "page": 9999, "vendor": "SKF"},
        )
        out = meta.to_chroma_dict()
        assert "source" not in out
        assert "page" not in out
        assert out["vendor"] == "SKF"

    def test_to_chroma_dict_strips_newlines_and_caps_length(self) -> None:
        long_value = "a" * 1000
        meta = DocumentMetadata(extra={"notes": f"line1\nline2\x07\n{long_value}"})
        out = meta.to_chroma_dict()
        assert "\n" not in out["notes"]
        assert "\x07" not in out["notes"]
        assert len(out["notes"]) <= 256

    def test_to_chroma_dict_drops_non_scalar_extras(self) -> None:
        meta = DocumentMetadata(
            extra={
                "valid_str": "ok",
                "valid_int": 42,
                "valid_bool": True,
                "bad_list": [1, 2, 3],
                "bad_dict": {"nested": "value"},
                "bad_none": None,
            }
        )
        out = meta.to_chroma_dict()
        assert out["valid_str"] == "ok"
        assert out["valid_int"] == 42
        assert out["valid_bool"] is True
        assert "bad_list" not in out
        assert "bad_dict" not in out
        assert "bad_none" not in out

    def test_to_chroma_dict_extra_cannot_override_explicit_field(self) -> None:
        # If both ``asset_id`` and ``extra["asset_id"]`` are set somehow, the
        # explicit field wins (the loader normally prevents this, but the
        # contract should still hold defensively).
        meta = DocumentMetadata(asset_id="P-201", extra={"asset_id": "HIJACK"})
        out = meta.to_chroma_dict()
        assert out["asset_id"] == "P-201"


class TestFromPathSidecar:
    """Loading a sidecar .meta.yaml file next to a source."""

    def test_sidecar_loaded(self, tmp_path: Path) -> None:
        source = tmp_path / "manual.pdf"
        source.write_bytes(b"%PDF-1.4 placeholder")
        sidecar = tmp_path / "manual.pdf.meta.yaml"
        sidecar.write_text(
            "asset_id: P-201\nequipment_class_code: PU\ndoc_type: manual\n",
            encoding="utf-8",
        )
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == "P-201"
        assert meta.equipment_class_code == "PU"
        assert meta.doc_type == "manual"

    def test_missing_sidecar_returns_inferred(self, tmp_path: Path) -> None:
        source = tmp_path / "P-201_manual.pdf"
        source.write_bytes(b"%PDF-1.4")
        meta = DocumentMetadata.from_path(source)
        # Inferred from filename
        assert meta.asset_id == "P-201"
        assert meta.doc_type == "manual"

    def test_sidecar_overrides_inferred(self, tmp_path: Path) -> None:
        source = tmp_path / "P-105_procedure.pdf"
        source.write_bytes(b"%PDF-1.4")
        sidecar = tmp_path / "P-105_procedure.pdf.meta.yaml"
        sidecar.write_text("asset_id: P-105B\ndoc_type: procedure\n", encoding="utf-8")
        meta = DocumentMetadata.from_path(source)
        # Sidecar wins
        assert meta.asset_id == "P-105B"
        assert meta.doc_type == "procedure"

    def test_malformed_sidecar_does_not_raise(self, tmp_path: Path) -> None:
        source = tmp_path / "manual.pdf"
        source.write_bytes(b"%PDF")
        sidecar = tmp_path / "manual.pdf.meta.yaml"
        sidecar.write_text("asset_id: P-201\n  bad indent: x\n: : :", encoding="utf-8")
        meta = DocumentMetadata.from_path(source)
        # No exception; returns empty metadata (with whatever inference yields)
        assert isinstance(meta, DocumentMetadata)

    def test_sidecar_with_list_root_ignored(self, tmp_path: Path) -> None:
        source = tmp_path / "random.pdf"
        source.write_bytes(b"%PDF")
        sidecar = tmp_path / "random.pdf.meta.yaml"
        sidecar.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        meta = DocumentMetadata.from_path(source)
        # Filename has no asset id or doc type keywords → sidecar contributes
        # nothing → empty metadata.
        assert meta == DocumentMetadata()


class TestFromPathFrontmatter:
    """YAML frontmatter inside .md / .txt files."""

    def test_markdown_frontmatter_loaded(self, tmp_path: Path) -> None:
        source = tmp_path / "guide.md"
        source.write_text(
            "---\nasset_id: COMP-301\ndoc_type: troubleshooting\n---\n\n"
            "# Guide\n\nBody text here.\n",
            encoding="utf-8",
        )
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == "COMP-301"
        assert meta.doc_type == "troubleshooting"

    def test_text_frontmatter_loaded(self, tmp_path: Path) -> None:
        source = tmp_path / "notes.txt"
        source.write_text(
            "---\nasset_id: V-007A\n---\nBody.\n",
            encoding="utf-8",
        )
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == "V-007A"

    def test_no_frontmatter_no_metadata(self, tmp_path: Path) -> None:
        source = tmp_path / "plain.md"
        source.write_text("# Plain doc\n\nNo frontmatter here.\n", encoding="utf-8")
        meta = DocumentMetadata.from_path(source)
        # No asset id in filename either
        assert meta.asset_id == ""


class TestFilenameInference:
    """Fallback inference from filename and parent dir when no sidecar/frontmatter."""

    def test_asset_id_detected_in_filename(self, tmp_path: Path) -> None:
        source = tmp_path / "P-201_manual.pdf"
        source.write_bytes(b"%PDF")
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == "P-201"

    def test_asset_id_with_letter_suffix(self, tmp_path: Path) -> None:
        source = tmp_path / "V-007A_datasheet.pdf"
        source.write_bytes(b"%PDF")
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == "V-007A"
        assert meta.doc_type == "datasheet"

    def test_doc_type_italian_keyword(self, tmp_path: Path) -> None:
        source = tmp_path / "procedura-cambio-olio.pdf"
        source.write_bytes(b"%PDF")
        meta = DocumentMetadata.from_path(source)
        assert meta.doc_type == "procedure"

    def test_parent_dir_contributes_to_inference(self, tmp_path: Path) -> None:
        sub = tmp_path / "P-201"
        sub.mkdir()
        source = sub / "manual.pdf"
        source.write_bytes(b"%PDF")
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == "P-201"
        assert meta.doc_type == "manual"

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        source = tmp_path / "random.pdf"
        source.write_bytes(b"%PDF")
        meta = DocumentMetadata.from_path(source)
        assert meta.asset_id == ""
        assert meta.doc_type == ""


class TestStripFrontmatter:
    """Helper used by the document loader to drop frontmatter from indexed text."""

    def test_strips_when_present(self) -> None:
        text = "---\nasset_id: P-201\n---\nBody content."
        assert strip_frontmatter(text) == "Body content."

    def test_passthrough_when_absent(self) -> None:
        text = "Just body content."
        assert strip_frontmatter(text) == text
