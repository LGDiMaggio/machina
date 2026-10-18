"""Tests for domain-aware prompts and context injection."""

from __future__ import annotations

import subprocess
import time
from pathlib import PureWindowsPath

import pytest

from machina.agent.entity_resolver import ResolvedEntity
from machina.agent.prompts import (
    DOC_DISPLAY_WINDOW,
    _safe_source,
    build_context_message,
    build_system_prompt,
    format_alarms_context,
    format_asset_context,
    format_document_results,
    format_resolved_entities,
    format_spare_parts_context,
    format_work_orders_context,
    render_for_llm,
    safe_source,
    safe_text,
)
from machina.connectors.docs.document_store import DocumentChunk
from machina.domain.alarm import Alarm, Severity
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.spare_part import SparePart
from machina.domain.work_order import Priority, WorkOrder, WorkOrderType


class TestBuildSystemPrompt:
    """Test system prompt construction."""

    def test_default_prompt(self) -> None:
        prompt = build_system_prompt()
        assert "maintenance assistant" in prompt.lower()
        assert "No plant configured" in prompt

    def test_followup_guideline_forbids_verbatim_repeat(self) -> None:
        # Guideline #9 must explicitly forbid repeating a previous answer
        # verbatim — weak models otherwise degenerate the "answer from the
        # conversation" instruction into echoing the prior turn.
        prompt = build_system_prompt().lower()
        assert "never repeat" in prompt
        assert "verbatim" in prompt

    def test_with_plant_context(self) -> None:
        prompt = build_system_prompt(
            plant_name="North Plant",
            asset_count=42,
        )
        assert "North Plant" in prompt
        assert "42" in prompt

    def test_with_capabilities(self) -> None:
        prompt = build_system_prompt(
            capabilities=["read_assets", "search_documents"],
        )
        assert "read_assets" in prompt
        assert "search_documents" in prompt

    def test_no_capabilities(self) -> None:
        prompt = build_system_prompt()
        assert "None configured" in prompt


class TestFormatAssetContext:
    """Test asset context formatting."""

    def test_basic_asset(self) -> None:
        asset = Asset(
            id="P-201",
            name="Cooling Water Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            location="Building A",
            criticality=Criticality.A,
        )
        text = format_asset_context(asset)
        assert "P-201" in text
        assert "Cooling Water Pump" in text
        assert "Building A" in text

    def test_asset_with_failure_modes(self) -> None:
        asset = Asset(
            id="P-201",
            name="Test",
            type=AssetType.ROTATING_EQUIPMENT,
            failure_modes=["BEAR-WEAR", "SEAL-LEAK"],
        )
        text = format_asset_context(asset)
        assert "BEAR-WEAR" in text


class TestFormatWorkOrders:
    """Test work order context formatting."""

    def test_empty_list(self) -> None:
        text = format_work_orders_context([])
        assert "No work orders" in text

    def test_with_orders(self) -> None:
        wos = [
            WorkOrder(
                id="WO-001",
                type=WorkOrderType.CORRECTIVE,
                priority=Priority.HIGH,
                asset_id="P-201",
                description="Fix the pump",
            ),
        ]
        text = format_work_orders_context(wos)
        assert "WO-001" in text
        assert "corrective" in text


class TestFormatAlarms:
    """Test alarm context formatting."""

    def test_empty_alarms(self) -> None:
        assert "No active alarms" in format_alarms_context([])

    def test_with_alarms(self) -> None:
        alarms = [
            Alarm(
                id="ALM-1",
                asset_id="P-201",
                severity=Severity.WARNING,
                parameter="vibration",
                value=7.8,
                threshold=6.0,
                unit="mm/s",
            ),
        ]
        text = format_alarms_context(alarms)
        assert "vibration" in text
        assert "7.8" in text


class TestFormatSpareParts:
    """Test spare parts context formatting."""

    def test_empty(self) -> None:
        assert "No spare parts" in format_spare_parts_context([])

    def test_in_stock(self) -> None:
        parts = [
            SparePart(
                sku="SKF-6310",
                name="Bearing 6310",
                stock_quantity=4,
            ),
        ]
        text = format_spare_parts_context(parts)
        assert "In stock" in text
        assert "SKF-6310" in text

    def test_out_of_stock(self) -> None:
        parts = [
            SparePart(
                sku="BELT-001",
                name="Belt",
                stock_quantity=0,
            ),
        ]
        text = format_spare_parts_context(parts)
        assert "Out of stock" in text


class TestFormatDocumentResults:
    """Test document results formatting."""

    def test_empty(self) -> None:
        assert "No relevant documents" in format_document_results([])

    def test_with_results(self) -> None:
        results = [
            {"content": "Step 1: Remove bearings...", "source": "manual.pdf", "page": 12},
        ]
        text = format_document_results(results)
        assert "manual.pdf" in text
        assert "12" in text

    def test_renders_the_citation_window(self) -> None:
        results = [
            {"content": "x", "source": f"doc_{i}.md", "page": 2, "section_title": "Seals"}
            for i in range(7)
        ]

        text = format_document_results(results)

        assert "Relevant Documents (7)" in text
        assert "  [1] Source: doc_0.md (p. 2) § Seals\n  x" in text
        assert f"doc_{DOC_DISPLAY_WINDOW - 1}.md" in text
        assert f"doc_{DOC_DISPLAY_WINDOW}.md" not in text


# Longer than the 60 characters a DocumentChunk repr previews.
_NESTED_CHUNK = DocumentChunk(
    r"Replace seal kit SK-7 on the drive end. Notes for the next shift are in "
    r"C:\Users\Mario Rossi\notes\seal.txt as usual.",
    source=r"D:\Plant\Manuals\pump.pdf",
    page=3,
    section_title="Bearings",
)


class TestRenderForLlm:
    """Workflow values rendered for the LLM carry no filesystem paths."""

    def test_chunks_render_by_file_name_without_a_citation_index(self) -> None:
        chunks = [
            DocumentChunk(
                r"Replace the seal. Notes in C:\Users\tedib\notes\seal.txt",
                source=r"D:\Plant\Manuals\pump.pdf",
                page=4,
                section_title="Seals",
            ),
            DocumentChunk("Torque table", source="/srv/docs/torque.xlsx", is_table=True),
        ]

        text = render_for_llm(chunks)

        assert text == (
            "Source: pump.pdf (p. 4) § Seals\nReplace the seal. Notes in seal.txt"
            "\n\nSource: torque.xlsx [TABLE]\nTorque table"
        )

    def test_a_single_chunk_and_a_tuple_render_the_same_way(self) -> None:
        chunk = DocumentChunk("x", source="/home/me/a.pdf")

        assert render_for_llm(chunk) == render_for_llm((chunk,)) == "Source: a.pdf\nx"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ([], "[]"),
            ({"export": r"C:\Users\tedib\out\v.csv"}, "{'export': 'v.csv'}"),
            ("Edit /etc/fstab", "Edit /etc/fstab"),
            (None, "None"),
        ],
    )
    def test_other_values_render_as_scrubbed_text(self, value: object, expected: str) -> None:
        assert render_for_llm(value) == expected

    def test_a_source_field_renders_as_a_file_name(self) -> None:
        assert render_for_llm("/srv/plant/docs/lube.md", key="source") == "lube.md"
        assert render_for_llm(r"D:\Plant\Manuals\x.pdf", key="Source") == "x.pdf"
        assert render_for_llm("/home/me", key="source") == "<document>"
        # Any other field keeps the free-text policy.
        assert render_for_llm("/etc/fstab", key="config") == "/etc/fstab"
        # A service URI in a source field (an Alarm's origin) keeps its topic.
        alarm_source = "mqtt://broker01/plant/line1/p201/vibration"
        assert render_for_llm(alarm_source, key="source") == alarm_source

    @pytest.mark.parametrize(
        "value",
        [r"C:\Users\Mario Rossi", PureWindowsPath(r"C:\Users\Mario Rossi"), "/home/luigi"],
    )
    def test_a_value_that_is_a_home_directory_becomes_a_placeholder(self, value: object) -> None:
        assert render_for_llm(value, key="home_dir") == "<path>"

    @pytest.mark.parametrize(
        "value",
        [
            {"results": [_NESTED_CHUNK], "count": 1},
            [None, _NESTED_CHUNK],
            ("hits", (_NESTED_CHUNK,)),
        ],
    )
    def test_chunks_nested_in_containers_render_as_blocks(self, value: object) -> None:
        text = render_for_llm(value)

        assert "Source: pump.pdf (p. 3) § Bearings\n" in text
        # The full text, not a repr's 60-character preview.
        assert (
            _NESTED_CHUNK.content.replace(r"C:\Users\Mario Rossi\notes\seal.txt", "seal.txt")
            in text
        )
        for fragment in ("Plant", "Mario", "Rossi", "DocumentChunk("):
            assert fragment not in text

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"source": r"D:\Plant\Manuals\x.pdf"}, "{'source': 'x.pdf'}"),
            ('{"source": "/srv/plant/docs/x.md", "page": 2}', '{"source": "x.md", "page": 2}'),
            ("Found(source='/srv/plant/docs/x.md', score=0.9)", "Found(source='x.md', score=0.9)"),
            ({"resource": "/srv/plant/docs/x.md"}, "{'resource': '/srv/plant/docs/x.md'}"),
        ],
    )
    def test_source_keys_in_rendered_text_are_reduced(self, value: object, expected: str) -> None:
        assert render_for_llm(value) == expected

    def test_a_source_name_with_an_apostrophe_loses_its_directory(self) -> None:
        """``repr`` switches to double quotes for such a value."""
        text = render_for_llm({"source": "/srv/docs/Manuale d'uso.pdf"})

        assert "/srv/docs" not in text
        assert text.startswith("{'source': ")

    @pytest.mark.parametrize(
        "value",
        [
            "/home/luigi/log.txt shows 7 mm/s vibration",
            r"C:\Users\mrossi\data.csv not found; check backup\2024 folder",
            "//FILESRV01/share/a.pdf was replaced by the 7 mm/s trend",
        ],
    )
    def test_a_value_that_starts_with_a_path_renders_like_free_text(self, value: str) -> None:
        text = render_for_llm(value)

        assert text == safe_text(value)
        for fragment in ("luigi", "mrossi", "FILESRV01"):
            assert fragment not in text

    @pytest.mark.parametrize(
        "value",
        ["ns=2;s=Line1/P201/Vibration", "SCADA/Line1/P201", "manuals/pump.md"],
    )
    def test_a_source_that_is_not_an_absolute_path_keeps_its_context(self, value: str) -> None:
        """An OPC-UA node id, a tag path or a relative path names no user or host."""
        assert render_for_llm(value, key="source") == value
        assert render_for_llm({"source": value}) == repr({"source": value})

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (r"C:\Users\Luigi di Maggio", "<path>"),
            (r"C:\Users\luigi di maggio x y\x.pdf", "x.pdf"),
            # A whole value that starts with a bare home is taken as one path.
            ("/home/luigi has 2 GB free", "<path>"),
        ],
    )
    def test_a_whole_value_led_by_a_path_is_scanned_as_one_path(
        self, value: str, expected: str
    ) -> None:
        assert render_for_llm(value) == expected

    def test_a_home_relative_source_loses_its_directory(self) -> None:
        assert render_for_llm("~/manuals/x.pdf", key="source") == "x.pdf"

    def test_a_nested_chunk_body_keeps_its_source_assignments(self) -> None:
        chunk = DocumentChunk("Config: source = '/etc/hosts'", source="m.pdf")

        nested = render_for_llm({"hits": [chunk, None]})

        assert "Source: m.pdf\nConfig: source = '/etc/hosts'" in nested
        assert render_for_llm([chunk]) == "Source: m.pdf\nConfig: source = '/etc/hosts'"

    def test_a_chunk_nested_deeper_than_the_copy_limit_loses_its_directory(self) -> None:
        value: object = _NESTED_CHUNK
        for _ in range(10):
            value = [value]

        text = render_for_llm(value)

        assert "pump.pdf" in text
        for fragment in ("Plant", "Manuals", "Mario"):
            assert fragment not in text

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("source='" + "a" * 300_000, id="unclosed-source-value"),
            pytest.param("{'source': '" + "\\\\" * 150_000, id="escaped-source-value"),
            pytest.param("C:\\Users\\" + "a b" * 100_000, id="long-whole-value"),
            pytest.param({"source": "/srv/" + "a/" * 100_000}, id="deep-source-path"),
            pytest.param({"hits": [_NESTED_CHUNK] * 10_000}, id="many-nested-chunks"),
        ],
    )
    def test_rendering_stays_linear_on_adversarial_input(self, value: object) -> None:
        start = time.perf_counter()
        render_for_llm(value)
        assert time.perf_counter() - start < 2.0

    def test_a_generator_output_is_not_consumed(self) -> None:
        chunks = (c for c in [_NESTED_CHUNK])

        render_for_llm(chunks)

        assert list(chunks) == [_NESTED_CHUNK]


class TestFormatResolvedEntities:
    """Test resolved entity formatting."""

    def test_empty(self) -> None:
        assert format_resolved_entities([]) == ""

    def test_with_entities(self) -> None:
        asset = Asset(id="P-201", name="Pump", type=AssetType.ROTATING_EQUIPMENT)
        ent = ResolvedEntity(asset, confidence=0.9, match_reason="name_match")
        text = format_resolved_entities([ent])
        assert "P-201" in text
        assert "90%" in text


class TestBuildContextMessage:
    """Test composite context message building."""

    def test_empty_context(self) -> None:
        assert build_context_message() == ""

    def test_with_asset(self) -> None:
        asset = Asset(
            id="P-201",
            name="Pump",
            type=AssetType.ROTATING_EQUIPMENT,
        )
        text = build_context_message(asset=asset)
        assert "P-201" in text

    def test_combined_context(self) -> None:
        asset = Asset(
            id="P-201",
            name="Pump",
            type=AssetType.ROTATING_EQUIPMENT,
        )
        text = build_context_message(
            asset=asset,
            work_orders=[],
            spare_parts=[],
        )
        assert "P-201" in text
        assert "No work orders" in text

    def test_with_alarms(self) -> None:
        alarms = [
            Alarm(
                id="ALM-1",
                asset_id="P-201",
                severity=Severity.WARNING,
                parameter="vibration",
                value=7.8,
                threshold=6.0,
                unit="mm/s",
            ),
        ]
        text = build_context_message(alarms=alarms)
        assert "vibration" in text

    def test_with_document_results(self) -> None:
        results = [
            {"content": "Replace bearing", "source": "manual.pdf", "page": 5},
        ]
        text = build_context_message(document_results=results)
        assert "manual.pdf" in text

    def test_full_context(self) -> None:
        """Exercise all branches at once."""
        asset = Asset(id="P-201", name="Pump", type=AssetType.ROTATING_EQUIPMENT)
        ent = ResolvedEntity(asset, confidence=0.9, match_reason="id")
        text = build_context_message(
            resolved_entities=[ent],
            asset=asset,
            work_orders=[],
            alarms=[],
            spare_parts=[],
            document_results=[],
        )
        assert "P-201" in text


# ---------------------------------------------------------------------------
# Path-leak sanitisation (regression for report-luigi U1)
# ---------------------------------------------------------------------------


class TestSafeSource:
    """The ``safe_source`` helper itself."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            # Filesystem paths -> basename
            (r"C:\Users\tedib\Desktop\manuals\manual.md", "manual.md"),
            (r"d:\data\report.pdf", "report.pdf"),
            ("/home/me/manuals/pump_p201_manual.md", "pump_p201_manual.md"),
            ("manuals/pump.md", "pump.md"),
            ("/file.txt", "file.txt"),  # POSIX root: last_sep == 0
            # Already-safe inputs pass through
            ("manual.md", "manual.md"),
            ("chunk_42", "chunk_42"),
            ("", ""),
            # Remote URLs are server-side identifiers — safe to expose
            ("https://example.com/path/to/doc.pdf", "https://example.com/path/to/doc.pdf"),
            ("http://example.com/x.pdf", "http://example.com/x.pdf"),
            ("s3://bucket/key/file.pdf", "s3://bucket/key/file.pdf"),
            ("gs://bucket/file.pdf", "gs://bucket/file.pdf"),
            ("ftp://example.com/x.pdf", "ftp://example.com/x.pdf"),
            # Local-by-protocol URIs are stripped of scheme then sanitised.
            ("file:///C:/Users/tedib/manuals/secret.md", "secret.md"),
            ("file:///home/me/manuals/secret.md", "secret.md"),
            ("scp://user@host/var/lib/secret.md", "secret.md"),
            ("smb://server/share/secret.md", "secret.md"),
            ("jar:///opt/jars/lib.jar", "lib.jar"),
            # Drive-relative Windows path (no separator) — strip the drive prefix
            ("C:filename.md", "filename.md"),
        ],
    )
    def test_safe_source_table(self, source: str, expected: str) -> None:
        assert safe_source(source) == expected

    def test_trailing_separator_returns_placeholder(self) -> None:
        """Trailing-separator paths previously yielded an empty basename.

        Empty ``Source:`` citations are worse than losing specificity —
        privacy is the priority, so we collapse to a generic placeholder.
        """
        assert safe_source("/home/me/") == "<document>"
        assert safe_source(r"C:\Users\foo\bar\\") == "<document>"

    def test_json_embedded_path_returns_placeholder(self) -> None:
        """JSON-shaped sources must not leak adjacent fields via rfind."""
        raw = '{"path": "/home/me/manuals/secret.md", "owner": "me"}'
        out = safe_source(raw)
        assert out == "<document>"
        assert "owner" not in out
        assert "/home/me" not in out

    def test_posix_repr_returns_placeholder(self) -> None:
        """Python ``repr(PosixPath(...))`` shapes must not leak quote suffixes."""
        out = safe_source("PosixPath('/home/me/manuals/secret.md')")
        assert out == "<document>"
        assert "secret.md')" not in out

    def test_bracket_or_brace_inputs_return_placeholder(self) -> None:
        """Other structured-string shapes also collapse to a placeholder."""
        assert safe_source("[/var/data/file.md]") == "<document>"
        assert safe_source("(quoted: /etc/secret.conf)") == "<document>"

    @pytest.mark.parametrize(
        "source",
        [
            "/home/me",
            "/Users/mario",
            r"C:\Users\Mario Rossi",
            r"\\FILESRV01\mrossi$",
            "//FILESRV01/share",
            "file://FILESRV01/share",
            "smb://FILESRV01/mrossi$",
            r"\\PC01\C$\Users\mrossi",
        ],
    )
    def test_a_bare_home_or_share_returns_placeholder(self, source: str) -> None:
        """Its last component is the user (or a share named after one)."""
        assert safe_source(source) == "<document>"

    @pytest.mark.parametrize(
        "source",
        [
            r"\\?\UNC\FILESRV01\share\x.pdf",
            r"\\?\C:\Users\Mario Rossi\x.pdf",
            "//FILESRV01/share/x.pdf",
            r"C:\Users\Mario Rossi\OneDrive - ACME S.p.A\x.pdf",
        ],
    )
    def test_long_path_and_forward_slash_shares_reduce_to_file_name(self, source: str) -> None:
        assert safe_source(source) == "x.pdf"

    def test_a_folder_named_home_below_a_profile_is_an_ordinary_folder(self) -> None:
        assert safe_source(r"C:\Users\me\Documents\home\guide.pdf") == "guide.pdf"

    def test_private_alias_still_resolves_to_same_function(self) -> None:
        """The ``_safe_source`` private alias remains for backwards compatibility."""
        assert _safe_source is safe_source


class TestSafeText:
    """``safe_text`` scrubs identity/infra paths embedded in free text while
    preserving instructional system paths (C2 hardening)."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # User-home paths are reduced to basename — the reported leak class.
            (
                r"See manual at C:\Users\tedib\Documents\pump_p201_manual.md for details",
                "See manual at pump_p201_manual.md for details",
            ),
            (
                "Saved to /home/me/manuals/pump.md by the loader",
                "Saved to pump.md by the loader",
            ),
            (
                "macOS path /Users/mario/docs/torque.pdf here",
                "macOS path torque.pdf here",
            ),
            # UNC shares (infra disclosure) reduced to basename.
            (
                r"Indexed \\FILESRV01\manuals\bearing.pdf overnight",
                "Indexed bearing.pdf overnight",
            ),
            # Instructional system paths are preserved (manual fidelity).
            ("Edit /etc/fstab then reboot", "Edit /etc/fstab then reboot"),
            ("Run /usr/bin/python3 to start", "Run /usr/bin/python3 to start"),
            (
                r"Installed under C:\Program Files\App\app.exe",
                r"Installed under C:\Program Files\App\app.exe",
            ),
            # No path → unchanged; empty → unchanged.
            ("Torque is 45 Nm on bolt M12", "Torque is 45 Nm on bolt M12"),
            ("", ""),
        ],
    )
    def test_safe_text_table(self, text: str, expected: str) -> None:
        assert safe_text(text) == expected

    def test_multiple_paths_in_one_string(self) -> None:
        raw = r"from C:\Users\a\x.md to /home/b/y.md"
        assert safe_text(raw) == "from x.md to y.md"

    def test_trailing_sentence_punctuation_not_swallowed_into_name(self) -> None:
        # A trailing period belongs to the sentence, not the filename.
        assert safe_text("see /home/me/notes.md.") == "see notes.md."

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"source": r"C:\Users\tedib\manuals\pump.pdf"}, "{'source': 'pump.pdf'}"),
            ({"source": r"\\FILESRV01\manuals\seal.pdf"}, "{'source': 'seal.pdf'}"),
            (
                [DocumentChunk("x", source=r"C:\Users\tedib\m\pump.pdf", page=3)],
                "[DocumentChunk(source='pump.pdf', page=3, text='x')]",
            ),
            # An escape sequence right before the share still ends a word.
            ("log\n" + r"\\FILESRV01\manuals\seal.pdf", r"'log\nseal.pdf'"),
        ],
    )
    def test_repr_escaped_paths_are_scrubbed(self, value: object, expected: str) -> None:
        """``repr()`` of a container doubles every backslash of a Windows path.

        Such reprs reach the LLM through workflow step outputs, so the doubled
        separators must not hide the path.
        """
        assert safe_text(repr(value)) == expected

    @pytest.mark.parametrize(
        "text",
        [
            repr(r"C:\Windows\System32\drivers\etc\hosts"),
            repr(r"C:\Program Files\App\app.exe"),
            r'"logs": "%ProgramData%\\Vendor\\logs"',
            r'"export": "..\\data\\export.csv"',
            r"match \\d+\\.\\d+ in the reading",
        ],
    )
    def test_escaped_text_that_is_not_a_share_is_kept(self, text: str) -> None:
        """Doubled backslashes alone (escaped system or relative paths, regexes)
        are not a UNC share."""
        assert safe_text(text) == text

    def test_unc_share_right_after_a_label_is_scrubbed(self) -> None:
        assert safe_text(r"Location:\\FILESRV01\manuals\seal.pdf") == "Location:seal.pdf"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                r"No such file: C:\Users\Mario Rossi\Documents\pump.pdf now",
                "No such file: pump.pdf now",
            ),
            (repr(r"C:\Users\Mario Rossi\Documents\pump.pdf"), "'pump.pdf'"),
            (r'{"p": "C:\\Users\\Mario Rossi\\pump.pdf"}', '{"p": "pump.pdf"}'),
            (
                str(
                    FileNotFoundError(
                        2, "No such file or directory", r"C:\Users\Mario Rossi\x.xlsx"
                    )
                ),
                "[Errno 2] No such file or directory: 'x.xlsx'",
            ),
            (r"C:\Users\mrossi\OneDrive - ACME S.p.A\Manuali\pump.pdf", "pump.pdf"),
            (repr(r"C:\Users\mrossi\OneDrive - ACME S.p.A\Manuali\pump.pdf"), "'pump.pdf'"),
            # How pathlib renders Windows paths: forward slashes, spaces kept.
            ("PureWindowsPath('C:/Users/Mario Rossi/x.pdf')", "PureWindowsPath('x.pdf')"),
            ("`C:\\Users\\Mario Rossi\\x.pdf`", "`x.pdf`"),
            ("Open C:/Users/Mario Rossi/Documents/pump.pdf now", "Open pump.pdf now"),
            ("Open /mnt/c/Users/Mario Rossi/Documents/pump.pdf now", "Open pump.pdf now"),
            (r"C:\Users\mrossi\OneDrive - Rossi Impianti S.r.l\Manuali\pump.pdf", "pump.pdf"),
            (r"Cannot open 'C:\Users\D'Amico\Documents\x.pdf'", "Cannot open 'x.pdf'"),
        ],
    )
    def test_user_names_with_spaces_are_scrubbed(self, text: str, expected: str) -> None:
        assert safe_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Permission denied: '/home/luigi'.", "Permission denied: '<path>'."),
            ("see /home/luigi and /Users/mario.", "see <path> and <path>."),
            (r"Profile C:\Users\mrossi\ missing", "Profile <path> missing"),
            (repr(r"C:\Users\Mario Rossi"), "'<path>'"),
            ("`C:\\Users\\Mario Rossi` is the profile", "`<path>` is the profile"),
            # A home folder reached through a share: the account is still last.
            (r"Access denied: \\PC01\C$\Users\mrossi", "Access denied: <path>"),
            (r"\\wsl.localhost\Ubuntu\home\luigi", "<path>"),
        ],
    )
    def test_a_bare_home_directory_becomes_a_placeholder(self, text: str, expected: str) -> None:
        """The last component of a home directory is the user name itself."""
        assert safe_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "//FILESRV01/share/a.pdf",
            "PureWindowsPath('//FILESRV01/share/a.pdf')",
            r"\\?\UNC\FILESRV01\share\a.pdf",
            repr(r"\\?\UNC\FILESRV01\share\a.pdf"),
            r"\\?\C:\Users\Mario Rossi\a.pdf",
            "PureWindowsPath('//?/C:/Users/Mario Rossi/a.pdf')",
            "file://FILESRV01/share/a.pdf",
            "file:///C:/Users/mrossi/a.pdf",
            "file:///C:/Users/Mario%20Rossi/a.pdf",
            "file:///home/luigi/a.pdf",
            "smb://FILESRV01/share/a.pdf",
            r"\\FILESRV01\Shared Docs\a.pdf",
            r"\\.\UNC\FILESRV01\share\a.pdf",
            "smb://FILESRV01:445/share/a.pdf",
            "sftp://mrossi@FILESRV01:2222/data/a.pdf",
            # Repr text escaped a second time doubles every backslash again.
            repr(repr(r"\\FILESRV01\share\a.pdf")),
            # A local path passed in a URL query, and a CSV neighbour of a URL.
            "https://viewer.local/open?path=C:/Users/mrossi/a.pdf",
            "https://wiki.local/doc,C:/Users/mrossi/a.pdf",
            # How pathname2url writes a UNC path, and a password in the authority.
            "file:////FILESRV01/share/a.pdf",
            "file://///FILESRV01/share/a.pdf",
            "smb://mrossi:S3cret@FILESRV01/share/a.pdf",
            "cifs://FILESRV01/share/a.pdf",
            "nfs://FILESRV01/share/a.pdf",
            "scp://mrossi@FILESRV01/data/a.pdf",
            "afp://FILESRV01/share/a.pdf",
            "file://localhost/C:/Users/mrossi/a.pdf",
            "file://localhost/home/luigi/a.pdf",
            # WebDAV UNC hosts carry @SSL / @port.
            r"\\FILESRV01@SSL\DavWWWRoot\share\a.pdf",
            r"\\FILESRV01@SSL@443\DavWWWRoot\a.pdf",
            "PureWindowsPath('//FILESRV01@SSL/DavWWWRoot/a.pdf')",
            # A database URL with an empty authority, and a macOS volume.
            "sqlite:////home/luigi/a.pdf",
            "/Volumes/Macintosh HD/Users/mrossi/a.pdf",
            "/cygdrive/c/Users/luigi/a.pdf",
        ],
    )
    def test_unc_and_uri_variants_lose_their_host(self, text: str) -> None:
        out = safe_text(text)
        assert "a.pdf" in out
        fragments = ("FILESRV01", "Mario", "Rossi", "mrossi", "luigi", "Users", "share", "S3cret")
        for fragment in fragments:
            assert fragment not in out

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (r"\\FILESRV01\mrossi$", "<path>"),
            ("//FILESRV01/share", "<path>"),
            ("file://FILESRV01/share", "<path>"),
            ("Mounted smb://FILESRV01:445 as S:", "Mounted <path> as S:"),
        ],
    )
    def test_a_share_without_a_path_becomes_a_placeholder(self, text: str, expected: str) -> None:
        """A share can be named after its user (``\\\\fs01\\mrossi$``)."""
        assert safe_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            r'"logs": "C:\\Windows\\System32\\winevt\\Logs"',
            r"..\data\export.csv and manuals/pump.md",
            r"%ProgramData%\Vendor\logs",
            "https://example.com/users/mario/profile and http://intranet/home/x",
            "s3://bucket/home/luigi/report.pdf, gs://bucket/Users/x.pdf",
            "ftp://ftp.example.com/pub/manual.pdf",
            "https://example.com/search?q=pump&lang=it",
            # Service endpoints are not filesystem paths.
            "mqtt://broker01/plant/line1/p201/vibration",
            "opc.tcp://plc-line2:4840/ns=2;s=Pump.Vibration",
            "file:///etc/fstab",
            # Relative paths, URL paths and REST paths are not homes.
            "Go to Settings/Users/Roles to add a technician.",
            "mqtt://broker01/cell3/robot1/home/state",
            "GET /api/users/42 returned 404",
            "GET /api/Users/42 and /v/users/42 returned 404",
            "mqtt://broker01/site/c/Users/state",
            # A scheme named in Markdown, and escaped newlines in notebook JSON.
            "Use ``smb://`` or ``file://`` URIs for shares.",
            r'"source": ["import os\n", "os.getcwd()\n"]',
            r"\\.\pipe\docker_engine",
            "x = total//count/2  # integer division",
            "```python\nopen('/etc/hosts')\n```\n\n**Bold** and `inline/code`",
            "Code BEAR-WEAR-01 on asset P-201, 7.1 mm/s.",
            "see https://docs.example.com/a?b=1, then /usr/local/bin/tool.",
        ],
    )
    def test_text_without_a_user_or_host_path_is_unchanged(self, text: str) -> None:
        assert safe_text(text) == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (r"C:\Users\me and C:\x", r"<path> and C:\x"),
            (r"Copy C:\Users\me to the folder Logs\2024", r"Copy <path> to the folder Logs\2024"),
            ("Readings in /home/me show 7 mm/s", "Readings in <path> show 7 mm/s"),
            # A slash or backslash a few words after the path is prose, not a folder.
            (
                r"Log at C:\Users\mrossi\vib.csv, 7.1 mm/s peak on P-201",
                "Log at vib.csv, 7.1 mm/s peak on P-201",
            ),
            (r"See C:\Users\mrossi\x.csv and/or the backup", "See x.csv and/or the backup"),
            (r"Log \\FILESRV01\share\log.txt on P-201\P-202", r"Log log.txt on P-201\P-202"),
        ],
    )
    def test_prose_after_a_path_is_kept(self, text: str, expected: str) -> None:
        assert safe_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (r"C:\Users\mrossi\x.pdf;C:\Users\luigi\y.pdf", "x.pdf;y.pdf"),
            (r"C:\Users\mrossi\x.pdf,C:\Users\luigi\y.pdf", "x.pdf,y.pdf"),
            (r"(C:\Users\mrossi\x.pdf)C:\Users\luigi\y.pdf", "(x.pdf)y.pdf"),
            (r"\\FILESRV01\share\x.pdf;C:\Users\luigi\y.pdf", "x.pdf;y.pdf"),
            (r"/home/mrossi/x.pdf;C:\Users\luigi\y.pdf", "x.pdf;y.pdf"),
            (repr(r"C:\Users\mrossi\x.pdf;C:\Users\luigi\y.pdf"), "'x.pdf;y.pdf'"),
            ("PATH=/usr/bin:/home/luigi/bin", "PATH=/usr/bin:bin"),
            ("//FS01/s/x.pdf;//FS02/s/y.pdf", "x.pdf;y.pdf"),
            (r"\\FS01\s\x.pdf;\\FS02\s\y.pdf", "x.pdf;y.pdf"),
            ("smb://FS01/s/x.pdf;smb://FS02/s/y.pdf", "x.pdf;y.pdf"),
            ("/home/a/x.pdf;/home/b/y.pdf", "x.pdf;y.pdf"),
            (r"smb://FILESRV01,C:\Users\luigi\y.pdf", "<path>,y.pdf"),
        ],
    )
    def test_paths_joined_without_whitespace_are_each_scrubbed(
        self, text: str, expected: str
    ) -> None:
        assert safe_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                str(
                    subprocess.CalledProcessError(
                        1, "/home/luigi/venv/bin/python run.py --out out/res.csv"
                    )
                ),
                "Command 'python run.py --out out/res.csv' returned non-zero exit status 1.",
            ),
            (
                '{"msg": "/home/luigi/data.csv has 7 mm/s peaks"}',
                '{"msg": "data.csv has 7 mm/s peaks"}',
            ),
            ("`/home/luigi/bin/run.sh --input data/x.csv`", "`run.sh --input data/x.csv`"),
            (r"'C:\Users\me\My Documents\pump manual.pdf'", "'pump manual.pdf'"),
        ],
    )
    def test_quoted_text_that_starts_with_a_path_keeps_its_words(
        self, text: str, expected: str
    ) -> None:
        """Only the user name, a OneDrive folder and the file name may hold spaces in quotes."""
        assert safe_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/srv/backup/home/luigi/x.pdf", "x.pdf"),
            ("Moved to /srv/backup/home/luigi.", "Moved to <path>."),
            ("/c/Users/Mario Rossi/Documents/x.pdf", "x.pdf"),
            (r"\\NAS01\homes\luigi", "<path>"),
            (r"\\FILESRV01\Profiles$\mrossi", "<path>"),
            (r"\\FS01\users$\mrossi", "<path>"),
            (r"\\FS01\home$\mrossi", "<path>"),
            (
                r"C:\Users\mrossi\OneDrive - Universita degli Studi di Napoli Federico II\x.pdf",
                "x.pdf",
            ),
            # Below a home head, a folder named users is an ordinary folder.
            ('File "/home/luigi/proj/users/models.py", line 3', 'File "models.py", line 3'),
            # ...unless what follows it has no extension: another account.
            ("/home/luigi/backup/home/mrossi", "<path>"),
            (r"\\FS01\profiles\mrossi", "<path>"),
            (r"\\FS01\homes$\mrossi", "<path>"),
            # A share host whose name ends in home or users is still a host.
            (r"\\nas-home\Users\mrossi", "<path>"),
            (r"\\NAS-HOME\homes\mrossi", "<path>"),
            ("smb://nas-users/Users/mrossi", "<path>"),
            # A user name may hold lowercase words; a deeper folder capitals and digits.
            (r"C:\Users\Maria de la Cruz\Documents\x.pdf", "x.pdf"),
            (repr(r"C:\Users\Maria de la Cruz\My Documents\x.pdf"), "'x.pdf'"),
            (r'{"p": "C:\\Users\\luigi di maggio\\New Folder\\x.pdf"}', '{"p": "x.pdf"}'),
            (r"C:\Users\mrossi\Backup 2024\x.pdf", "x.pdf"),
            (r"C:\Users\me\Projekt Čapek\x.pdf", "x.pdf"),
            (r"C:\Users\me\Отчёт Петров\x.pdf", "x.pdf"),
            (
                r"C:\Users\mrossi\OneDrive - Uno Due Tre Quattro Cinque Sei Sette Otto\x.pdf",
                "x.pdf",
            ),
            # A home after a compiler option letter.
            ("gcc -I/home/luigi/include/x.h", "gcc -Ix.h"),
        ],
    )
    def test_more_home_and_share_layouts(self, text: str, expected: str) -> None:
        assert safe_text(text) == expected

    def test_a_json_neighbour_of_a_url_is_still_scrubbed(self) -> None:
        raw = r'{"url":"https://x.com/a","p":"C:\\Users\\me\\x.pdf"}'
        assert safe_text(raw) == r'{"url":"https://x.com/a","p":"x.pdf"}'

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("\\" * 300_000, id="backslash-run"),
            pytest.param(("\\\\" + "a" * 50 + " ") * 5_000, id="unc-host-no-share"),
            pytest.param(
                ("C:" + "\\" * 1_000 + "Users" + "\\" * 1_000) * 100, id="drive-separator-runs"
            ),
            pytest.param("\\\\a" + ("\\" * 3 + "b") * 75_000, id="unc-odd-separators"),
            pytest.param("a\\\\" * 100_000, id="word-double-backslash"),
            pytest.param("C:\\\\" * 75_000, id="drive-double-backslash"),
            pytest.param(("C:\\Users\\" + "a\\" * 10 + " ") * 10_000, id="many-profile-paths"),
            pytest.param("C:\\Users\\a" + " b" * 150_000, id="spaced-words-no-separator"),
            pytest.param("C:\\Users\\a " + "b" * 300_000, id="long-word-after-space"),
            pytest.param("'C:\\Users\\" + "a b" * 100_000, id="unclosed-single-quote"),
            pytest.param("'C:\\Users\\" + "a'b" * 100_000, id="apostrophes-in-quote"),
            pytest.param('"/home/a ' + "'/home/a " * 30_000, id="nested-unclosed-quotes"),
            pytest.param("/" * 300_000, id="slash-run"),
            pytest.param("//a/" * 75_000, id="slash-unc-run"),
            pytest.param("file://" * 40_000, id="scheme-run"),
            pytest.param("smb://" + "a" * 300_000, id="share-uri-long-host"),
            pytest.param("/home/" + "a /" * 100_000, id="home-spaced"),
            pytest.param("`C:\\Users\\" + "x " * 100_000, id="unclosed-backtick"),
            pytest.param("https://" + "a" * 300_000, id="long-url"),
            pytest.param("https://a/" + "C" * 300_000, id="url-drive-lookahead"),
            pytest.param("C:\\Users\\" + "OneDrive - a " * 25_000, id="onedrive-words"),
            pytest.param("'C:\\Users\\x" + "\\OneDrive -  a" * 200, id="quoted-onedrive-spaces"),
            pytest.param("'" + "/home" * 50_000, id="quoted-home-runs"),
            pytest.param('"' + "/Users" * 50_000, id="quoted-users-runs"),
            pytest.param("'C:\\Users\\" + "\\" * 50_000, id="quoted-drive-backslashes"),
            pytest.param("/a" * 100_000, id="posix-segments-no-home"),
            pytest.param("/" + "ab/" * 100_000, id="posix-deep-no-home"),
            pytest.param("/%" * 100_000, id="posix-percent-segments"),
            pytest.param(" -I/a" * 50_000, id="option-flag-starts"),
            pytest.param("/Volumes/" + "a b/" * 50_000, id="volume-prefix"),
            pytest.param("x.pdf;" * 50_000, id="list-separators"),
        ],
    )
    def test_matching_stays_linear_on_adversarial_input(self, text: str) -> None:
        """A backtracking blow-up would take minutes on these; linear takes milliseconds."""
        start = time.perf_counter()
        safe_text(text)
        assert time.perf_counter() - start < 2.0


class TestFormatDocumentResultsSanitization:
    """The user-facing context formatter must strip directory components."""

    def test_windows_path_replaced_with_basename(self) -> None:
        results = [
            {
                "content": "Step 1: Remove the bearing",
                "source": r"C:\Users\tedib\Desktop\Scuola\manuals\pump_p201_manual.md",
                "page": 5,
            },
        ]
        text = format_document_results(results)
        assert "pump_p201_manual.md" in text
        assert "C:\\Users" not in text
        assert "tedib" not in text

    def test_posix_path_replaced_with_basename(self) -> None:
        results = [
            {"content": "Inspect the gasket", "source": "/home/me/manuals/pump.md", "page": 12},
        ]
        text = format_document_results(results)
        assert "pump.md" in text
        assert "/home/me/manuals" not in text

    def test_file_uri_replaced_with_basename(self) -> None:
        results = [
            {
                "content": "x",
                "source": "file:///C:/Users/tedib/Desktop/secret.md",
                "page": 1,
            },
        ]
        text = format_document_results(results)
        assert "secret.md" in text
        assert "file://" not in text
        assert "C:/Users" not in text


class TestSystemPromptFirewall:
    """The system prompt must explicitly forbid path / system disclosure."""

    def test_path_disclosure_clause_present(self) -> None:
        # Stable substring asserting the firewall rule exists.  If the
        # clause is reworded, update this assertion deliberately —
        # silent removal must be visible in the diff.
        assert "absolute file paths" in build_system_prompt()

    def test_clause_explicitly_calls_out_directory(self) -> None:
        assert "director" in build_system_prompt().lower()


class TestSystemPromptCapabilityHonesty:
    """The prompt must require declining actions outside the capability surface.

    Regression for the live-testing confabulation where the agent claimed it
    could "register a failure mode in the CMMS" — no such tool or write path
    exists. The constraint is capability-GENERIC: it covers any off-surface
    promise, not just failure modes.
    """

    def test_decline_clause_present_with_capabilities(self) -> None:
        # Stable substring asserting the constraint exists. If the clause is
        # reworded, update this deliberately — silent removal must be
        # visible in the diff.
        prompt = build_system_prompt(capabilities=["read_assets", "search_documents"])
        assert "COMPLETE set of actions" in prompt
        assert "Never promise, simulate, or imply" in prompt

    def test_clause_sits_after_the_live_capability_list(self) -> None:
        # The constraint leans on the live capability list injected just
        # above it — "what you CAN do instead" must refer to that list.
        prompt = build_system_prompt(capabilities=["read_assets"])
        assert prompt.index("read_assets") < prompt.index("COMPLETE set of actions")

    def test_clause_is_capability_generic(self) -> None:
        # No hardcoded mention of the incident's specifics: the constraint
        # must cover ANY unsupported action, so it never names failure modes.
        prompt = build_system_prompt()
        start = prompt.index("COMPLETE set of actions")
        end = prompt.index("## Registered Workflows")
        assert "failure mode" not in prompt[start:end].lower()
