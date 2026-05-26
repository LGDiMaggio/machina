"""Tests for domain-aware prompts and context injection."""

from __future__ import annotations

import pytest

from machina.agent.entity_resolver import ResolvedEntity
from machina.agent.prompts import (
    _safe_source,
    build_context_message,
    build_system_prompt,
    format_alarms_context,
    format_asset_context,
    format_document_results,
    format_resolved_entities,
    format_spare_parts_context,
    format_work_orders_context,
    safe_source,
)
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

    def test_private_alias_still_resolves_to_same_function(self) -> None:
        """The ``_safe_source`` private alias remains for backwards compatibility."""
        assert _safe_source is safe_source


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
