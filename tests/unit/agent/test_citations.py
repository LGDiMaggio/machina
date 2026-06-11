"""Tests for the citation parser and AgentResponse domain type.

The citation contract is index-based: the model cites a retrieved
document by its visible ``[n]`` marker. The parser maps ``[n]`` to the
chunk shown at that display position, with a source/page fallback for
models that name a document by filename instead. The rendered citation
source is always taken from the registry and ``safe_source``-sanitised,
so an absolute path never leaks back through a citation.
"""

from __future__ import annotations

from machina.agent.citations import parse_response, renormalize_markers, strip_markers
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

    def test_unterminated_block_opener_stripped(self) -> None:
        # Weak models sometimes open the block but never close it. The bare
        # ``<citations>`` tag must NOT leak into the rendered answer.
        text = "Just an answer.\n<citations>"
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert "<citations>" not in rendered
        assert rendered == "Just an answer."
        assert cites == []

    def test_unterminated_block_still_parses_indices(self) -> None:
        # An unterminated block with valid entries still yields citations.
        text = "Body [1].\n<citations>\n[1]"
        rendered, cites = parse_response(text, _registry(), _ordered())
        assert "<citations>" not in rendered
        assert rendered == "Body [1]."
        assert len(cites) == 1
        assert cites[0].chunk_id == "abc123"


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


# ---------------------------------------------------------------------------
# Egress renormalization (U3) — pure helper
# ---------------------------------------------------------------------------


def _cite(chunk_id: str, source: str = "", page: int = 0) -> Citation:
    return Citation(chunk_id=chunk_id, source=source or f"{chunk_id}.md", page=page)


class TestRenormalizeMarkers:
    """renormalize_markers: 1..N by first appearance, fail-closed stripping."""

    def test_raw_indices_renumbered_by_first_appearance(self) -> None:
        ordered = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]
        cites = [_cite("c6", "bearing.md", 12), _cite("c8", "lube.md", 7)]
        text = "Replace every 8000 h [6]. Grease every 500 h [8]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "Replace every 8000 h [1]. Grease every 500 h [2]."
        assert [c.chunk_id for c in reordered] == ["c6", "c8"]

    def test_first_appearance_order_wins_over_parsed_order(self) -> None:
        # Block listed c2 before c1, but the prose mentions [1] first — the
        # displayed numbering follows the prose, and the list is reordered.
        ordered = ["c1", "c2"]
        cites = [_cite("c2"), _cite("c1")]
        text = "Alpha [1], beta [2]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "Alpha [1], beta [2]."
        assert [c.chunk_id for c in reordered] == ["c1", "c2"]

    def test_duplicate_marker_shares_one_number(self) -> None:
        ordered = ["c1", "c2", "c3"]
        cites = [_cite("c3")]
        text = "Fact [3]. Repeated fact [3]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "Fact [1]. Repeated fact [1]."
        assert len(reordered) == 1

    def test_two_raw_indices_to_same_chunk_share_one_number(self) -> None:
        # Dedup collision: a re-retrieved chunk sits at two display positions;
        # the map is marker -> chunk_id -> number, never positional.
        ordered = ["c1", "cdup", "c3", "c4", "c5", "c6", "cdup"]
        cites = [_cite("cdup", "vib.md", 5)]
        text = "Threshold [2]. Investigate above it [7]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "Threshold [1]. Investigate above it [1]."
        assert len(reordered) == 1

    def test_out_of_range_marker_stripped(self) -> None:
        ordered = ["c1", "c2", "c3"]
        cites = [_cite("c2")]
        text = "Valid [2]. Dangling claim [9]."
        out, _ = renormalize_markers(text, cites, ordered)
        assert out == "Valid [1]. Dangling claim."

    def test_in_range_marker_without_block_entry_stripped(self) -> None:
        # [2] maps to a registered chunk, but the model never cited it in the
        # block — fail-closed: stripped exactly like out-of-range, never
        # synthesized into a Citation.
        ordered = ["c1", "c2"]
        cites = [_cite("c1")]
        text = "Cited [1]. Uncited [2]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "Cited [1]. Uncited."
        assert [c.chunk_id for c in reordered] == ["c1"]

    def test_marker_on_empty_display_slot_stripped(self) -> None:
        ordered = ["", "c2"]
        cites = [_cite("c2")]
        text = "Ghost [1] and real [2]."
        out, _ = renormalize_markers(text, cites, ordered)
        assert out == "Ghost and real [1]."

    def test_block_only_citations_appended_after_inline(self) -> None:
        ordered = ["c1", "c2", "c3"]
        cites = [_cite("c1"), _cite("c2"), _cite("c3")]
        text = "Only one inline mention [3]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "Only one inline mention [1]."
        # c3 took number 1 (inline); c1 and c2 follow in parsed order.
        assert [c.chunk_id for c in reordered] == ["c3", "c1", "c2"]

    def test_zero_citations_text_byte_identical(self) -> None:
        text = "Bracketed [1] text [9] with no citations at all."
        out, reordered = renormalize_markers(text, [], ["c1"])
        assert out == text
        assert reordered == []

    def test_fenced_code_block_literal_untouched(self) -> None:
        ordered = ["c1"]
        cites = [_cite("c1")]
        text = "Read the register [1].\n\n```\nvalue = registers[1]\n```\n"
        out, _ = renormalize_markers(text, cites, ordered)
        assert "registers[1]" in out
        assert out.startswith("Read the register [1].")

    def test_marker_between_two_fenced_blocks_renormalized(self) -> None:
        # The inter-fence region is PROSE: a real citation marker there must
        # renormalize, while bracket literals inside BOTH fences stay
        # byte-identical (the fence detector must not pair the first fence's
        # close with the second fence's open and swallow the prose between).
        ordered = ["c1", "c2", "c3"]
        cites = [_cite("c3", "manual.md", 4)]
        text = (
            "```\nfirst = registers[1]\n```\n\n"
            "The threshold is documented [3].\n\n"
            "```\nsecond = registers[2]\n```\n"
        )
        out, reordered = renormalize_markers(text, cites, ordered)
        assert "registers[1]" in out
        assert "registers[2]" in out
        assert "The threshold is documented [1]." in out
        assert "[3]" not in out
        assert [c.chunk_id for c in reordered] == ["c3"]

    def test_inline_backtick_literal_untouched(self) -> None:
        ordered = ["c1", "c2"]
        cites = [_cite("c2")]
        text = "Use `arr[1]` as shown [2]."
        out, _ = renormalize_markers(text, cites, ordered)
        assert out == "Use `arr[1]` as shown [1]."

    def test_markdown_link_untouched(self) -> None:
        ordered = ["c1"]
        cites = [_cite("c1")]
        text = "See [1](https://example.com/spec) and the manual [1]."
        out, _ = renormalize_markers(text, cites, ordered)
        assert "[1](https://example.com/spec)" in out
        assert out.endswith("the manual [1].")

    def test_high_indices_with_placeholder_slots(self) -> None:
        # Absolute indices into a multi-call turn map, with empty-string
        # displayed-but-unregistered slots in between.
        ordered = ["", "", "", "", "", "", "c7", "", "", "", "", "c12"]
        cites = [_cite("c7"), _cite("c12")]
        text = "First fact [7]. Second fact [12]."
        out, reordered = renormalize_markers(text, cites, ordered)
        assert out == "First fact [1]. Second fact [2]."
        assert [c.chunk_id for c in reordered] == ["c7", "c12"]

    def test_four_digit_bracket_not_a_marker(self) -> None:
        ordered = ["c1"]
        cites = [_cite("c1")]
        text = "Torque to [1000] Nm per spec [1]."
        out, _ = renormalize_markers(text, cites, ordered)
        assert "[1000]" in out


class TestStripMarkers:
    """strip_markers: history fail-closed marker removal."""

    def test_strips_all_markers(self) -> None:
        text = "Fact one [1]. Fact two [2], related [12]."
        assert strip_markers(text) == "Fact one. Fact two, related."

    def test_preserves_history_source_note(self) -> None:
        text = "Answer [1].\n\n[Sources used in this answer: pump.md]"
        assert strip_markers(text) == "Answer.\n\n[Sources used in this answer: pump.md]"

    def test_preserves_code_spans_and_links(self) -> None:
        text = "Use `arr[1]` [1] and see [2](https://x).\n```\nregs[3]\n```"
        out = strip_markers(text)
        assert "`arr[1]`" in out
        assert "[2](https://x)" in out
        assert "regs[3]" in out
        assert " [1]" not in out

    def test_no_markers_returns_text_unchanged(self) -> None:
        text = "No markers here, just [brackets] and [a1] codes."
        assert strip_markers(text) == text

    def test_adjacent_markers_both_stripped(self) -> None:
        assert strip_markers("Claim [1][2].") == "Claim."
