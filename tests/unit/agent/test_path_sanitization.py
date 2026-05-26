"""Tests for path-leak sanitization in LLM-visible payloads.

Regression suite for the path-leakage defect: absolute filesystem
paths must never reach the LLM context, the tool result payload, or
the final response text via citation metadata.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.agent.prompts import (
    _safe_source,
    build_system_prompt,
    format_document_results,
    safe_source,
)
from machina.agent.runtime import Agent
from machina.connectors.capabilities import Capability
from machina.connectors.docs.document_store import DocumentChunk


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


class TestRuntimeContextPayloadSanitization:
    """End-to-end: the search_documents tool result strips paths."""

    @pytest.mark.asyncio
    async def test_search_documents_tool_strips_absolute_paths(self) -> None:
        """The tool result fed back to the LLM must contain basenames only."""
        # Mock document connector that returns chunks with absolute paths,
        # file URIs, and one JSON-shaped source (which must collapse to
        # the opaque placeholder rather than leak adjacent fields).
        leaky_chunks = [
            DocumentChunk(
                content="Remove the four bolts on the bearing housing.",
                source=r"C:\Users\tedib\Desktop\manuals\pump_p201_manual.md",
                page=5,
            ),
            DocumentChunk(
                content="Apply LOTO before opening the casing.",
                source="/home/me/manuals/safety.md",
                page=2,
            ),
            DocumentChunk(
                content="Bypass attempt via file URI.",
                source="file:///C:/Users/tedib/secret.md",
                page=1,
            ),
        ]
        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({Capability.SEARCH_DOCUMENTS})
        mock_conn.search = AsyncMock(return_value=leaky_chunks)
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()

        agent = Agent(
            name="test",
            connectors=[mock_conn],
            channels=[],
            llm="openai:gpt-4o",  # never actually called in this test
        )

        result: list[dict[str, Any]] = await agent._execute_tool(
            "search_documents",
            {"query": "bearing replacement"},
        )

        # Result must be a list of dicts with sanitised sources.
        assert isinstance(result, list)
        assert len(result) == 3
        sources = [r["source"] for r in result]
        assert sources == ["pump_p201_manual.md", "safety.md", "secret.md"]
        # The original paths and their scheme prefixes must NOT appear.
        for r in result:
            assert "C:\\Users" not in r["source"]
            assert "/home/me" not in r["source"]
            assert "tedib" not in r["source"]
            assert "file://" not in r["source"]
