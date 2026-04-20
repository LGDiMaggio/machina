"""Action tracing for debugging and auditing agent behaviour.

Records every action the agent takes (connector calls, LLM requests,
tool invocations) as structured trace entries.  Useful for debugging,
compliance logging, and understanding agent reasoning.

v0.3 additions: conversation_id, LLM token/cost fields, subscriber
callbacks, and redacting_dump_json() for safe JSONL export.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from pydantic import BaseModel, Field

_REDACT_PATTERNS = re.compile(
    r"(token|password|secret|api_key|client_secret|authorization)",
    re.IGNORECASE,
)


class TraceEntry(BaseModel):
    """A single traced action performed by the agent."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action: str = Field(..., description="Action name (e.g. 'llm_call', 'connector_query')")
    connector: str = Field(default="", description="Connector involved, if any")
    asset_id: str = Field(default="", description="Asset context, if any")
    operation: str = Field(default="", description="Specific operation name")
    input_summary: str = Field(default="", description="Brief summary of input")
    output_summary: str = Field(default="", description="Brief summary of output")
    duration_ms: float = Field(default=0.0, description="Duration in milliseconds")
    success: bool = Field(default=True, description="Whether the action succeeded")
    error: str = Field(default="", description="Error message if action failed")
    metadata: dict[str, Any] = Field(default_factory=dict)

    # v0.3 — LLM cost tracking (OpenTelemetry-compatible naming)
    conversation_id: str = Field(default="", description="Conversation/session identifier")
    prompt_tokens: int = Field(default=0, description="Input tokens consumed")
    completion_tokens: int = Field(default=0, description="Output tokens generated")
    total_tokens: int = Field(default=0, description="Total tokens (prompt + completion)")
    usd_cost: float = Field(default=0.0, description="Estimated USD cost of this action")
    model: str = Field(default="", description="LLM model identifier")

    def redacting_dump_json(self, *, max_summary_chars: int = 2000) -> str:
        """Serialize to JSON with secret redaction and summary truncation.

        Unlike ``model_dump_json()``, this method:
        - Redacts metadata keys matching sensitive patterns
        - Truncates ``input_summary`` and ``output_summary``
        """
        data = self.model_dump(mode="json")

        for key in list(data.get("metadata", {})):
            if _REDACT_PATTERNS.search(key):
                data["metadata"][key] = "***REDACTED***"

        for field in ("input_summary", "output_summary"):
            val = data.get(field, "")
            if len(val) > max_summary_chars:
                data[field] = val[:max_summary_chars] + "...[truncated]"

        import json

        return json.dumps(data, default=str)


class ActionTracer:
    """Records and stores agent action traces.

    Example:
        ```python
        tracer = ActionTracer()
        with tracer.trace("llm_call", operation="complete") as span:
            result = await llm.complete(messages)
            span.output_summary = result[:100]
        ```
    """

    def __init__(self, *, max_entries: int = 1000) -> None:
        self._entries: list[TraceEntry] = []
        self._max_entries = max_entries
        self._subscribers: list[Callable[[TraceEntry], Any]] = []

    @property
    def entries(self) -> list[TraceEntry]:
        """All recorded trace entries."""
        return list(self._entries)

    def subscribe(self, callback: Callable[[TraceEntry], Any]) -> None:
        """Register a callback invoked on every new entry."""
        self._subscribers.append(callback)

    def record(self, entry: TraceEntry) -> None:
        """Add a trace entry and notify subscribers."""
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries :]
        for cb in self._subscribers:
            try:  # noqa: SIM105
                cb(entry)
            except Exception:
                pass

    def trace(
        self,
        action: str,
        *,
        connector: str = "",
        asset_id: str = "",
        operation: str = "",
        conversation_id: str = "",
        **metadata: Any,
    ) -> _TraceContext:
        """Context manager that records timing and outcome.

        Args:
            action: Action category (e.g. ``"llm_call"``, ``"connector_query"``).
            connector: Connector name involved.
            asset_id: Asset context.
            operation: Specific operation name.
            conversation_id: Conversation/session identifier.
            **metadata: Extra metadata to attach.

        Returns:
            A context manager yielding a :class:`TraceEntry` that will
            be finalised and recorded on exit.
        """
        entry = TraceEntry(
            action=action,
            connector=connector,
            asset_id=asset_id,
            operation=operation,
            conversation_id=conversation_id,
            metadata=metadata,
        )
        return _TraceContext(self, entry)

    def clear(self) -> None:
        """Remove all recorded entries."""
        self._entries.clear()

    def summary(self) -> list[dict[str, Any]]:
        """Return a serialisable summary of all entries."""
        return [e.model_dump() for e in self._entries]


class _TraceContext:
    """Context manager for tracing a single action with timing."""

    def __init__(self, tracer: ActionTracer, entry: TraceEntry) -> None:
        self._tracer = tracer
        self.entry = entry
        self._start: float = 0.0

    def __enter__(self) -> TraceEntry:
        self._start = time.perf_counter()
        return self.entry

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        elapsed = (time.perf_counter() - self._start) * 1000
        self.entry.duration_ms = round(elapsed, 2)
        if exc_val is not None:
            self.entry.success = False
            self.entry.error = str(exc_val)
        self._tracer.record(self.entry)
