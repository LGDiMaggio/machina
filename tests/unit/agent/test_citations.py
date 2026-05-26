"""Tests for the citation parser and AgentResponse domain type."""

from __future__ import annotations

from machina.agent.citations import parse_response
from machina.domain.citation import AgentResponse, Citation

# ---------------------------------------------------------------------------
# Domain type
# ---------------------------------------------------------------------------


class TestCitationModel:
    def test_inline_marker_with_page(self) -> None:
        c = Citation(chunk_id="abc", source="manuals/pump.pdf", page=42)
        assert c.inline_marker() == "[manuals/pump.pdf:42]"

    def test_inline_marker_without_page(self) -> None:
        c = Citation(chunk_id="abc", source="manuals/pump.pdf", page=0)
        assert c.inline_marker() == "[manuals/pump.pdf]"

    def test_inline_marker_falls_back_to_chunk_id(self) -> None:
        c = Citation(chunk_id="abc")
        assert c.inline_marker() == "[abc]"


class TestAgentResponse:
    def test_defaults(self) -> None:
        r = AgentResponse()
        assert r.text == ""
        assert r.citations == []

    def test_with_citations(self) -> None:
        r = AgentResponse(
            text="Hello [src:1].",
            citations=[Citation(chunk_id="abc", source="src", page=1)],
        )
        assert len(r.citations) == 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _registry() -> dict[str, dict[str, object]]:
    return {
        "abc123": {"source": "manuals/pump.pdf", "page": 42, "content": "..."},
        "def456": {"source": "manuals/comp.md", "page": 0, "content": "..."},
    }


class TestParseResponse:
    def test_no_block_returns_text_unchanged(self) -> None:
        text = "Just an answer with no citations."
        rendered, cites = parse_response(text, _registry())
        assert rendered == text
        assert cites == []

    def test_extracts_block_and_strips_it(self) -> None:
        text = (
            "Replace the bearing every 2000 hours [manuals/pump.pdf:42].\n\n"
            "<citations>\n"
            "abc123 | manuals/pump.pdf | 42\n"
            "</citations>\n"
        )
        rendered, cites = parse_response(text, _registry())
        assert "<citations>" not in rendered
        assert "[manuals/pump.pdf:42]" in rendered
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"
        assert cites[0].source == "manuals/pump.pdf"
        assert cites[0].page == 42

    def test_inline_markers_preserved(self) -> None:
        text = "First claim [a:1]. Second claim [b:2].\n<citations>\nabc123 | a | 1\n</citations>"
        rendered, _ = parse_response(text, _registry())
        assert "[a:1]" in rendered
        assert "[b:2]" in rendered

    def test_unknown_chunk_id_filtered_out(self) -> None:
        text = (
            "Body.\n<citations>\n"
            "abc123 | manuals/pump.pdf | 42\n"
            "unknown_id | fake | 0\n"
            "</citations>"
        )
        rendered, cites = parse_response(text, _registry())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"
        assert "<citations>" not in rendered

    def test_duplicate_chunk_ids_deduplicated(self) -> None:
        text = (
            "Body.\n<citations>\n"
            "abc123 | manuals/pump.pdf | 42\n"
            "abc123 | manuals/pump.pdf | 42\n"
            "</citations>"
        )
        _, cites = parse_response(text, _registry())
        assert len(cites) == 1

    def test_empty_block_returns_empty_citations(self) -> None:
        text = "Body.\n<citations>\n\n</citations>"
        rendered, cites = parse_response(text, _registry())
        assert cites == []
        assert "<citations>" not in rendered

    def test_falls_back_to_registry_metadata_when_fields_missing(self) -> None:
        # Block only carries chunk_id; source and page should be filled from registry.
        text = "Body.\n<citations>\nabc123\n</citations>"
        _, cites = parse_response(text, _registry())
        assert len(cites) == 1
        assert cites[0].source == "manuals/pump.pdf"
        assert cites[0].page == 42

    def test_malformed_page_value_does_not_raise(self) -> None:
        text = "Body.\n<citations>\nabc123 | manuals/pump.pdf | notanumber\n</citations>"
        _, cites = parse_response(text, _registry())
        # Falls back to registry page
        assert cites[0].page == 42

    def test_handles_block_without_trailing_newline(self) -> None:
        text = "Body.\n<citations>\nabc123 | a | 1</citations>"
        rendered, cites = parse_response(text, _registry())
        assert len(cites) == 1
        assert "<citations>" not in rendered

    def test_comment_lines_in_block_ignored(self) -> None:
        text = "Body.\n<citations>\n# this is a comment\nabc123 | a | 1\n</citations>"
        _, cites = parse_response(text, _registry())
        assert len(cites) == 1

    def test_chunk_with_zero_page_renders_correctly(self) -> None:
        text = "Body.\n<citations>\ndef456 | manuals/comp.md | 0\n</citations>"
        _, cites = parse_response(text, _registry())
        assert len(cites) == 1
        assert cites[0].page == 0

    def test_multiple_blocks_all_stripped_and_merged(self) -> None:
        text = (
            "First claim [a:1].\n"
            "<citations>\nabc123 | manuals/pump.pdf | 42\n</citations>\n"
            "Second claim [b:2].\n"
            "<citations>\ndef456 | manuals/comp.md | 0\n</citations>"
        )
        rendered, cites = parse_response(text, _registry())
        assert "<citations>" not in rendered
        assert "[a:1]" in rendered
        assert "[b:2]" in rendered
        chunk_ids = {c.chunk_id for c in cites}
        assert chunk_ids == {"abc123", "def456"}

    def test_pipe_in_source_path_preserved(self) -> None:
        registry = {
            "abc123": {
                "source": "manuals/Section 5 | Maintenance.pdf",
                "page": 7,
                "content": "...",
            },
        }
        text = "Body.\n<citations>\nabc123 | manuals/Section 5 | Maintenance.pdf | 7\n</citations>"
        _, cites = parse_response(text, registry)
        assert len(cites) == 1
        assert "Section 5" in cites[0].source
        assert "Maintenance" in cites[0].source
        assert cites[0].page == 7
