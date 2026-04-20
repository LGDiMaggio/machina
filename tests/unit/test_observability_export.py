"""Tests for JSONL exporter — redaction, rotation, file permissions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from machina.observability.export.jsonl import JSONLExporter
from machina.observability.tracing import ActionTracer, TraceEntry


class TestJSONLExporter:
    def test_writes_one_line_per_entry(self, tmp_path: Path) -> None:
        tracer = ActionTracer()
        exporter = JSONLExporter(tmp_path / "traces")
        exporter.attach(tracer)

        tracer.record(TraceEntry(action="action_1"))
        tracer.record(TraceEntry(action="action_2"))

        files = list((tmp_path / "traces").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2

    def test_each_line_is_valid_json(self, tmp_path: Path) -> None:
        tracer = ActionTracer()
        exporter = JSONLExporter(tmp_path / "traces")
        exporter.attach(tracer)

        tracer.record(TraceEntry(action="test"))

        files = list((tmp_path / "traces").glob("*.jsonl"))
        for line in files[0].read_text().strip().split("\n"):
            data = json.loads(line)
            assert data["action"] == "test"

    def test_daily_rotation_filename(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        tracer = ActionTracer()
        exporter = JSONLExporter(tmp_path / "traces", rotate_daily=True)
        exporter.attach(tracer)

        tracer.record(TraceEntry(action="test"))

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected = tmp_path / "traces" / f"traces-{today}.jsonl"
        assert expected.exists()

    def test_no_rotation_uses_single_file(self, tmp_path: Path) -> None:
        tracer = ActionTracer()
        exporter = JSONLExporter(tmp_path / "traces", rotate_daily=False)
        exporter.attach(tracer)

        tracer.record(TraceEntry(action="test"))

        expected = tmp_path / "traces" / "traces.jsonl"
        assert expected.exists()

    def test_creates_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "traces"
        assert not target.exists()
        JSONLExporter(target)
        assert target.exists()

    def test_redacts_secrets_in_metadata(self, tmp_path: Path) -> None:
        tracer = ActionTracer()
        exporter = JSONLExporter(tmp_path / "traces")
        exporter.attach(tracer)

        tracer.record(
            TraceEntry(
                action="test",
                metadata={"api_key": "super-secret", "normal_field": "visible"},
            )
        )

        files = list((tmp_path / "traces").glob("*.jsonl"))
        data = json.loads(files[0].read_text().strip())
        assert data["metadata"]["api_key"] == "***REDACTED***"
        assert data["metadata"]["normal_field"] == "visible"

    def test_truncates_long_summaries(self, tmp_path: Path) -> None:
        tracer = ActionTracer()
        exporter = JSONLExporter(tmp_path / "traces", max_summary_chars=50)
        exporter.attach(tracer)

        tracer.record(TraceEntry(action="test", input_summary="x" * 200))

        files = list((tmp_path / "traces").glob("*.jsonl"))
        data = json.loads(files[0].read_text().strip())
        assert len(data["input_summary"]) < 200
        assert data["input_summary"].endswith("...[truncated]")


class TestTraceEntryRedaction:
    def test_redacting_dump_json_basic(self) -> None:
        entry = TraceEntry(action="test", metadata={"password": "secret123"})
        line = entry.redacting_dump_json()
        data = json.loads(line)
        assert data["metadata"]["password"] == "***REDACTED***"

    def test_redacting_dump_json_preserves_normal_fields(self) -> None:
        entry = TraceEntry(
            action="llm_call",
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            usd_cost=0.005,
        )
        line = entry.redacting_dump_json()
        data = json.loads(line)
        assert data["model"] == "gpt-4o"
        assert data["prompt_tokens"] == 100
        assert data["usd_cost"] == 0.005

    def test_conversation_id_preserved(self) -> None:
        entry = TraceEntry(action="test", conversation_id="conv-abc-123")
        line = entry.redacting_dump_json()
        data = json.loads(line)
        assert data["conversation_id"] == "conv-abc-123"
