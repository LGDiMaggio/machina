"""Action tracing for debugging and auditing agent behaviour.

Records every action the agent takes (connector calls, LLM requests,
tool invocations) as structured trace entries.  Useful for debugging,
compliance logging, and understanding agent reasoning.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


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

    @property
    def entries(self) -> list[TraceEntry]:
        """All recorded trace entries."""
        return list(self._entries)

    def record(self, entry: TraceEntry) -> None:
        """Add a trace entry."""
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries :]

    def trace(
        self,
        action: str,
        *,
        connector: str = "",
        asset_id: str = "",
        operation: str = "",
        **metadata: Any,
    ) -> _TraceContext:
        """Context manager that records timing and outcome.

        Args:
            action: Action category (e.g. ``"llm_call"``, ``"connector_query"``).
            connector: Connector name involved.
            asset_id: Asset context.
            operation: Specific operation name.
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
