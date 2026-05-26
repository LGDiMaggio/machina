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
)
from machina.agent.runtime import Agent
from machina.connectors.capabilities import Capability
from machina.connectors.docs.document_store import DocumentChunk


class TestSafeSource:
    """The ``_safe_source`` helper itself."""

    def test_windows_absolute_path_stripped_to_basename(self) -> None:
        assert _safe_source(r"C:\Users\tedib\Desktop\manuals\manual.md") == "manual.md"

    def test_windows_path_lowercase_drive(self) -> None:
        assert _safe_source(r"d:\data\report.pdf") == "report.pdf"

    def test_posix_absolute_path_stripped_to_basename(self) -> None:
        assert _safe_source("/home/me/manuals/pump_p201_manual.md") == "pump_p201_manual.md"

    def test_relative_path_stripped_to_basename(self) -> None:
        assert _safe_source("manuals/pump.md") == "pump.md"

    def test_bare_filename_passes_through(self) -> None:
        assert _safe_source("manual.md") == "manual.md"

    def test_opaque_doc_id_passes_through(self) -> None:
        assert _safe_source("chunk_42") == "chunk_42"

    def test_url_passes_through_unchanged(self) -> None:
        assert _safe_source("https://example.com/path/to/doc.pdf") == (
            "https://example.com/path/to/doc.pdf"
        )

    def test_empty_passes_through(self) -> None:
        assert _safe_source("") == ""

    def test_trailing_separator(self) -> None:
        # Edge case: a path that ends with a separator yields the empty
        # basename — fall back to the original to avoid a useless empty
        # citation.  Not a security issue (no leak), just usability.
        assert _safe_source("/home/me/") in {"/home/me/", ""}


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
        # Mock document connector that returns chunks with absolute paths.
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

        result: Any = await agent._execute_tool(
            "search_documents",
            {"query": "bearing replacement"},
        )

        # Result must be a list of dicts with sanitised sources.
        assert isinstance(result, list)
        assert len(result) == 2
        sources = [r["source"] for r in result]
        assert sources == ["pump_p201_manual.md", "safety.md"]
        # The original paths must NOT appear in any field.
        for r in result:
            assert "C:\\Users" not in r["source"]
            assert "/home/me" not in r["source"]
            assert "tedib" not in r["source"]
