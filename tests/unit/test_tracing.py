"""Tests for the ActionTracer and TraceEntry."""

from machina.observability.tracing import ActionTracer, TraceEntry


class TestTraceEntry:
    """Test TraceEntry model."""

    def test_defaults(self) -> None:
        entry = TraceEntry(action="test")
        assert entry.action == "test"
        assert entry.success is True
        assert entry.error == ""
        assert entry.duration_ms == 0.0
        assert entry.connector == ""

    def test_with_details(self) -> None:
        entry = TraceEntry(
            action="connector_query",
            connector="cmms",
            asset_id="P-201",
            operation="read_work_orders",
            success=False,
            error="Connection timeout",
        )
        assert entry.connector == "cmms"
        assert entry.asset_id == "P-201"
        assert entry.success is False


class TestActionTracer:
    """Test the ActionTracer recording and context manager."""

    def test_record(self) -> None:
        tracer = ActionTracer()
        entry = TraceEntry(action="test_action")
        tracer.record(entry)
        assert len(tracer.entries) == 1
        assert tracer.entries[0].action == "test_action"

    def test_trace_context_manager(self) -> None:
        tracer = ActionTracer()
        with tracer.trace("llm_call", operation="complete") as span:
            span.output_summary = "Got a response"
            # Simulate some work
            _ = sum(range(100))

        assert len(tracer.entries) == 1
        entry = tracer.entries[0]
        assert entry.action == "llm_call"
        assert entry.operation == "complete"
        assert entry.duration_ms >= 0
        assert entry.success is True

    def test_trace_records_errors(self) -> None:
        tracer = ActionTracer()
        try:
            with tracer.trace("failing_action"):
                raise ValueError("Something went wrong")
        except ValueError:
            pass

        assert len(tracer.entries) == 1
        entry = tracer.entries[0]
        assert entry.success is False
        assert "Something went wrong" in entry.error

    def test_max_entries(self) -> None:
        tracer = ActionTracer(max_entries=5)
        for i in range(10):
            tracer.record(TraceEntry(action=f"action_{i}"))
        assert len(tracer.entries) == 5
        assert tracer.entries[0].action == "action_5"

    def test_clear(self) -> None:
        tracer = ActionTracer()
        tracer.record(TraceEntry(action="test"))
        tracer.clear()
        assert len(tracer.entries) == 0

    def test_summary(self) -> None:
        tracer = ActionTracer()
        tracer.record(TraceEntry(action="test_action", connector="cmms"))
        summary = tracer.summary()
        assert len(summary) == 1
        assert summary[0]["action"] == "test_action"
        assert summary[0]["connector"] == "cmms"
