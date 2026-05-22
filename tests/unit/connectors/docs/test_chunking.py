"""Tests for the SectionAwareSplitter."""

from __future__ import annotations

from machina.connectors.docs.chunking import (
    MatchChunk,
    ParentSection,
    SectionAwareSplitter,
)


class TestMarkdownHeadings:
    def test_multi_step_procedure_stays_in_one_parent(self) -> None:
        """A Markdown section with N steps yields 1 parent and N match chunks all pointing to it."""
        text = (
            "# Pump P-201 Manual\n\n"
            "## Bearing Replacement Procedure\n\n"
            "Step 1: Lock out / Tag out the motor.\n"
            "Step 2: Remove coupling.\n"
            "Step 3: Use bearing puller to remove old bearings.\n"
            "Step 4: Heat new SKF 6310 bearings to 110 C.\n"
            "Step 5: Slide bearings onto shaft.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=60, chunk_overlap=0)

        parents, matches = splitter.split(text, source="P-201_manual.md")

        # The procedure section is one parent — not split across parents.
        proc_parents = [p for p in parents if p.title == "Bearing Replacement Procedure"]
        assert len(proc_parents) == 1
        proc = proc_parents[0]
        assert "Step 1" in proc.text and "Step 5" in proc.text

        # Every match chunk belonging to this section points to the same parent_id.
        proc_matches = [m for m in matches if m.parent_id == proc.parent_id]
        assert len(proc_matches) >= 2  # small chunk_size forces multiple matches
        assert all(m.section_title == "Bearing Replacement Procedure" for m in proc_matches)

    def test_two_sections_yield_two_parents(self) -> None:
        text = (
            "## Section A\n\n"
            "Alpha content one.\n"
            "Alpha content two.\n\n"
            "## Section B\n\n"
            "Beta content one.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)

        parents, matches = splitter.split(text)

        titles = {p.title for p in parents}
        assert "Section A" in titles
        assert "Section B" in titles

        # Each match's parent_id resolves to exactly one parent
        parent_ids = {p.parent_id for p in parents}
        assert all(m.parent_id in parent_ids for m in matches)

    def test_nested_headings_create_distinct_parents(self) -> None:
        text = "# Top\n\n## Child A\n\nAlpha alpha.\n\n## Child B\n\nBeta beta.\n"
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        titles = {p.title for p in parents}
        assert {"Child A", "Child B"}.issubset(titles)


class TestFlatTextHeuristics:
    def test_all_caps_heading_creates_section(self) -> None:
        text = (
            "INTRODUCTION\n\n"
            "This pump is a centrifugal pump used in process service.\n\n"
            "BEARING REPLACEMENT PROCEDURE\n\n"
            "Step 1: Lock out.\n"
            "Step 2: Remove coupling.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        titles = {p.title for p in parents}
        assert "BEARING REPLACEMENT PROCEDURE" in titles

    def test_numbered_section_creates_section(self) -> None:
        text = (
            "1. INTRODUCTION\n\n"
            "Pump description here.\n\n"
            "2.1 Bearing Replacement\n\n"
            "Step 1: Lock out.\n"
            "Step 2: Remove coupling.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        titles = {p.title for p in parents}
        # The numbered heading should be detected (verbatim, with the number prefix).
        assert any("Bearing Replacement" in t for t in titles)


class TestFallback:
    def test_no_headings_falls_back_to_recursive(self) -> None:
        text = "Plain paragraph one.\n\nPlain paragraph two.\n\nPlain paragraph three.\n"
        splitter = SectionAwareSplitter(chunk_size=50, chunk_overlap=0)
        parents, matches = splitter.split(text)

        # In fallback mode, parent == chunk (each match points to a parent
        # that contains the same text).
        assert len(parents) == len(matches)
        for m in matches:
            parent = next(p for p in parents if p.parent_id == m.parent_id)
            assert parent.text == m.text


class TestFencedCodeBlocks:
    def test_hash_lines_inside_fenced_block_are_not_headings(self) -> None:
        """Markdown lines starting with '#' inside ``` fences must not split sections."""
        text = (
            "# Manual\n\n"
            "## Bearing Replacement\n\n"
            "Step 1: Lock out.\n\n"
            "```bash\n"
            "# Replace bearing\n"
            "## Step 2\n"
            "echo replacing\n"
            "```\n\n"
            "Step 2: Remove coupling.\n"
            "Step 3: Use puller.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        titles = [p.title for p in parents]
        # The fence content should NOT produce phantom headings.
        assert "Replace bearing" not in titles
        assert "Step 2" not in titles
        # The legitimate heading should be the only ## section.
        assert "Bearing Replacement" in titles


class TestHeadingTightening:
    def test_numbered_body_line_is_not_promoted_to_heading(self) -> None:
        """`1. Lock out` mid-paragraph must not become a section heading."""
        text = (
            "# Procedure\n\n"
            "Follow these steps in order:\n"
            "1. Lock out the motor.\n"
            "2. Remove coupling.\n"
            "3. Pull bearings.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        # Only "Procedure" should be a parent — numbered list items
        # without blank-line context must not spawn sections.
        titles = [p.title for p in parents]
        assert titles == ["Procedure"]

    def test_numbered_heading_with_blank_neighbour_strips_prefix(self) -> None:
        """`2.1 Bearing Replacement` becomes a heading; title is the text after the number."""
        text = "Intro paragraph.\n\n2.1 Bearing Replacement\n\nStep 1: Lock out.\n"
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        titles = [p.title for p in parents]
        # Title is the text after the number, not the whole line.
        assert "Bearing Replacement" in titles
        assert "2.1 Bearing Replacement" not in titles

    def test_all_caps_callout_inside_prose_is_not_a_heading(self) -> None:
        """ALL-CAPS lines mid-paragraph (warning callouts) must not split sections."""
        text = (
            "# Procedure\n\n"
            "Before starting, ensure isolation.\n"
            "DO NOT OPERATE THE MOTOR\n"
            "Continue with the steps below.\n"
            "Step 1: Lock out.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, _ = splitter.split(text)
        titles = [p.title for p in parents]
        assert "DO NOT OPERATE THE MOTOR" not in titles


class TestOffsets:
    def test_match_offsets_are_consistent_with_parent_text(self) -> None:
        """MatchChunk.start_offset + parent.title_offset locates the match in parent.text."""
        text = (
            "## Bearing Replacement\n\n"
            "alpha alpha alpha alpha alpha.\n"
            "beta beta beta beta beta.\n"
            "TARGET TARGET TARGET TARGET.\n"
            "omega omega omega omega.\n"
        )
        splitter = SectionAwareSplitter(chunk_size=40, chunk_overlap=0)
        parents, matches = splitter.split(text)
        assert parents and matches
        parent = parents[0]
        target_matches = [m for m in matches if "TARGET" in m.text]
        assert target_matches, "TARGET should appear in some match"
        m = target_matches[0]
        absolute_pos = parent.title_offset + m.start_offset
        # The match.text should appear at the recorded offset inside parent.text.
        assert parent.text[absolute_pos : absolute_pos + len(m.text)] == m.text


class TestStructuredSplit:
    """split_structured consumes a pre-parsed ParsedDocument."""

    def test_sections_become_parents_with_match_chunks(self) -> None:
        from machina.connectors.docs.parsing import ParsedDocument, Section

        parsed = ParsedDocument(
            source="m.pdf",
            sections=(
                Section(
                    title="Bearing Replacement",
                    level=2,
                    text="Step 1: Lock out. Step 2: Remove coupling. Step 3: Pull bearings.",
                    page_range=(4, 5),
                ),
            ),
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        parents, matches = splitter.split_structured(parsed)

        assert len(parents) == 1
        assert parents[0].title == "Bearing Replacement"
        assert parents[0].level == 2
        assert "Step 1" in parents[0].text and "Step 3" in parents[0].text
        # All matches point to this parent and carry the start page.
        assert all(m.parent_id == parents[0].parent_id for m in matches)
        assert all(m.page == 4 for m in matches)

    def test_table_is_emitted_as_atomic_match(self) -> None:
        from machina.connectors.docs.parsing import ParsedDocument, TableBlock

        table_md = "| Fastener | Torque (Nm) |\n|---|---|\n| M10 | 45 |\n| M12 | 80 |"
        parsed = ParsedDocument(
            source="m.pdf",
            tables=(TableBlock(text=table_md, page=4, caption="Torque Specs"),),
        )
        splitter = SectionAwareSplitter(chunk_size=20, chunk_overlap=0)  # tiny on purpose
        parents, matches = splitter.split_structured(parsed)

        # Exactly one parent + one match — the table must NEVER be split.
        table_parents = [p for p in parents if p.title == "Torque Specs"]
        assert len(table_parents) == 1
        table_matches = [m for m in matches if m.parent_id == table_parents[0].parent_id]
        assert len(table_matches) == 1
        assert table_matches[0].atomic is True
        assert "M10" in table_matches[0].text
        assert "M12" in table_matches[0].text

    def test_section_with_inline_table_keeps_table_atomic(self) -> None:
        """Prose section + sibling table — splitter emits both, table not split."""
        from machina.connectors.docs.parsing import (
            ParsedDocument,
            Section,
            TableBlock,
        )

        parsed = ParsedDocument(
            source="m.pdf",
            sections=(
                Section(
                    title="Torque Specs",
                    level=2,
                    text="Refer to the table below for fastener torques.",
                    page_range=(4, 4),
                ),
            ),
            tables=(
                TableBlock(
                    text="| Fastener | Torque |\n|---|---|\n| M10 | 45 Nm |",
                    page=4,
                ),
            ),
        )
        splitter = SectionAwareSplitter(chunk_size=200, chunk_overlap=0)
        _parents, matches = splitter.split_structured(parsed)

        prose = [m for m in matches if "table below" in m.text]
        atomic = [m for m in matches if m.atomic]
        assert prose, "prose match should exist"
        assert len(atomic) == 1
        assert "M10" in atomic[0].text


class TestApiShape:
    def test_match_and_parent_dataclasses_exist(self) -> None:
        # Smoke test that the public dataclasses can be constructed.
        p = ParentSection(parent_id="x", title="t", level=1, text="hello")
        m = MatchChunk(
            text="hello", parent_id="x", section_title="t", section_level=1, index_in_section=0
        )
        assert p.parent_id == m.parent_id == "x"
