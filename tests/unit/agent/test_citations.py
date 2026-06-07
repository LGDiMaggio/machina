"""Tests for the citation parser and AgentResponse domain type.

The citation contract is index-based: the model cites a retrieved
document by its visible ``[n]`` marker. The parser maps ``[n]`` to the
chunk shown at that display position, with a source/page fallback for
models that name a document by filename instead. The rendered citation
source is always taken from the registry and ``safe_source``-sanitised,
so an absolute path never leaks back through a citation.
"""

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
# Parser fixtures
# ---------------------------------------------------------------------------


def _registry() -> dict[str, dict[str, object]]:
    return {
        "abc123": {"source": "manuals/pump.pdf", "page": 42, "content": "..."},
        "def456": {"source": "manuals/comp.md", "page": 0, "content": "..."},
    }


def _ordered() -> list[str]:
    # Display order matching ``_registry``: [1] -> abc123, [2] -> def456.
    return ["abc123", "def456"]


# ---------------------------------------------------------------------------
# Index-based contract (primary)
# ---------------------------------------------------------------------------


class TestIndexContract:
    def test_no_block_returns_text_unchanged(self) -> None:
        text = "Just an answer with no citations."
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert rendered == text
        assert cites == []

    def test_bracketed_index_resolves(self) -> None:
        # AE5: model emits [1] and [2] in the block; both resolve.
        text = "Body [1] and more [2].\n<citations>\n[1]\n[2]\n</citations>"
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert "<citations>" not in rendered
        # Inline markers preserved.
        assert "[1]" in rendered
        assert "[2]" in rendered
        chunk_ids = {c.chunk_id for c in cites}
        assert chunk_ids == {"abc123", "def456"}

    def test_bare_integer_index_resolves(self) -> None:
        # ``1 | source | page`` form with the index as the first field.
        text = "Body.\n<citations>\n1 | pump.pdf | 42\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"
        assert cites[0].page == 42

    def test_index_with_note_resolves(self) -> None:
        text = "Body.\n<citations>\n[2] | a follow-up note\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].chunk_id == "def456"

    def test_off_by_k_guard_with_empty_chunk_id_slot(self) -> None:
        # An earlier displayed row has an empty chunk_id (shown as [1] but
        # skipped by registration). [2] must resolve to the SECOND displayed
        # chunk, not the first registered one.
        registry = {"def456": {"source": "manuals/comp.md", "page": 0, "content": "..."}}
        ordered = ["", "def456"]  # [1] displayed-but-unregistered, [2] -> def456
        text = "Body [2].\n<citations>\n[2]\n</citations>"
        _, cites = parse_response(text, registry, ordered)
        assert len(cites) == 1
        assert cites[0].chunk_id == "def456"

    def test_out_of_range_index_dropped(self) -> None:
        # Regression guard: [9] with no fallback match -> warning, dropped.
        text = "Body.\n<citations>\n[9]\n</citations>"
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert cites == []
        assert "<citations>" not in rendered

    def test_index_pointing_at_empty_slot_dropped(self) -> None:
        # [1] maps to an empty-string slot -> unresolvable, dropped.
        ordered = ["", "def456"]
        registry = {"def456": {"source": "manuals/comp.md", "page": 0, "content": "..."}}
        text = "Body.\n<citations>\n[1]\n</citations>"
        _, cites = parse_response(text, registry, ordered)
        assert cites == []


# ---------------------------------------------------------------------------
# Source / page fallback
# ---------------------------------------------------------------------------


class TestSourceFallback:
    def test_bare_source_filename_resolves(self) -> None:
        # AE6: model cites a bare filename instead of an index.
        text = "Body.\n<citations>\npump.pdf\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"

    def test_bare_source_case_insensitive_basename(self) -> None:
        # Citing a different-case basename of a directoried registry source.
        text = "Body.\n<citations>\nPUMP.PDF\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"

    def test_source_page_form_resolves(self) -> None:
        text = "Body.\n<citations>\npump.pdf:42\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"
        assert cites[0].page == 42

    def test_unknown_source_dropped(self) -> None:
        text = "Body.\n<citations>\nnope.pdf\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert cites == []

    def test_chunk_id_echo_still_resolves(self) -> None:
        # Backward tolerance: a model that echoes a raw chunk_id still works.
        text = "Body.\n<citations>\nabc123\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"


# ---------------------------------------------------------------------------
# Ambiguity rule (two chunks sharing a source)
# ---------------------------------------------------------------------------


class TestAmbiguity:
    def _shared_source_registry(self) -> dict[str, dict[str, object]]:
        return {
            "c1": {"source": "manuals/pump.pdf", "page": 1, "content": "..."},
            "c2": {"source": "manuals/pump.pdf", "page": 2, "content": "..."},
        }

    def test_index_resolves_unambiguously(self) -> None:
        ordered = ["c1", "c2"]
        text = "Body.\n<citations>\n[2]\n</citations>"
        _, cites = parse_response(text, self._shared_source_registry(), ordered)
        assert len(cites) == 1
        assert cites[0].chunk_id == "c2"

    def test_bare_source_ambiguous_is_dropped(self) -> None:
        # Rule: a bare-source fallback resolves only when the basename is
        # unique. Two chunks share ``pump.pdf`` -> ambiguous -> dropped.
        ordered = ["c1", "c2"]
        text = "Body.\n<citations>\npump.pdf\n</citations>"
        _, cites = parse_response(text, self._shared_source_registry(), ordered)
        assert cites == []

    def test_source_page_disambiguates_shared_source(self) -> None:
        # A page narrows the shared source to a single chunk.
        ordered = ["c1", "c2"]
        text = "Body.\n<citations>\npump.pdf:2\n</citations>"
        _, cites = parse_response(text, self._shared_source_registry(), ordered)
        assert len(cites) == 1
        assert cites[0].chunk_id == "c2"


# ---------------------------------------------------------------------------
# Security: rendered source is safe_source-sanitised
# ---------------------------------------------------------------------------


class TestSourceSanitisation:
    def test_rendered_source_strips_absolute_path(self) -> None:
        registry = {
            "abc123": {
                "source": "C:\\Users\\me\\manuals\\pump.pdf",
                "page": 3,
                "content": "...",
            },
        }
        text = "Body.\n<citations>\n[1]\n</citations>"
        _, cites = parse_response(text, registry, ["abc123"])
        assert len(cites) == 1
        # No absolute path leak — only the basename survives.
        assert cites[0].source == "pump.pdf"
        assert "Users" not in cites[0].source
        assert "\\" not in cites[0].source

    def test_model_supplied_source_text_is_ignored(self) -> None:
        # Even if the model writes an absolute path in the source field,
        # the rendered citation comes from the (sanitised) registry value.
        registry = {"abc123": {"source": "manuals/pump.pdf", "page": 42, "content": "..."}}
        text = "Body.\n<citations>\n1 | /home/secret/leak.pdf | 42\n</citations>"
        _, cites = parse_response(text, registry, ["abc123"])
        assert len(cites) == 1
        assert cites[0].source == "pump.pdf"


# ---------------------------------------------------------------------------
# Tolerance / formatting edge cases (preserved behaviour)
# ---------------------------------------------------------------------------


class TestTolerance:
    def test_inline_markers_preserved(self) -> None:
        text = "First [1]. Second [2].\n<citations>\n[1]\n</citations>"
        rendered, _ = parse_response(text, _registry(), _ordered())
        assert "[1]" in rendered
        assert "[2]" in rendered

    def test_duplicate_indices_deduplicated(self) -> None:
        text = "Body.\n<citations>\n[1]\n[1]\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1

    def test_empty_block_returns_empty_citations(self) -> None:
        text = "Body.\n<citations>\n\n</citations>"
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert cites == []
        assert "<citations>" not in rendered

    def test_index_fills_source_and_page_from_registry(self) -> None:
        text = "Body.\n<citations>\n[1]\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].source == "pump.pdf"
        assert cites[0].page == 42

    def test_malformed_page_value_does_not_raise(self) -> None:
        text = "Body.\n<citations>\n1 | pump.pdf | notanumber\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        # Falls back to registry page.
        assert cites[0].page == 42

    def test_handles_block_without_trailing_newline(self) -> None:
        text = "Body.\n<citations>\n[1]</citations>"
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert "<citations>" not in rendered

    def test_comment_lines_in_block_ignored(self) -> None:
        text = "Body.\n<citations>\n# this is a comment\n[1]\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1

    def test_chunk_with_zero_page_renders_correctly(self) -> None:
        text = "Body.\n<citations>\n[2]\n</citations>"
        _, cites = parse_response(text, _registry(), _ordered())
        assert len(cites) == 1
        assert cites[0].page == 0

    def test_multiple_blocks_all_stripped_and_merged(self) -> None:
        text = (
            "First [1].\n"
            "<citations>\n[1]\n</citations>\n"
            "Second [2].\n"
            "<citations>\n[2]\n</citations>"
        )
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert "<citations>" not in rendered
        assert "[1]" in rendered
        assert "[2]" in rendered
        chunk_ids = {c.chunk_id for c in cites}
        assert chunk_ids == {"abc123", "def456"}

    def test_missing_ordered_argument_still_allows_source_fallback(self) -> None:
        # ordered_chunks defaults to empty; the source fallback still works.
        text = "Body.\n<citations>\npump.pdf\n</citations>"
        _, cites = parse_response(text, _registry())
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"
