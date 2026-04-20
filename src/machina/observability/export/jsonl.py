"""JSONL trace exporter with mandatory redaction and daily rotation.

Subscribes to an :class:`ActionTracer` and writes one redacted JSON
line per trace entry.  Output directory is created with mode ``0o700``
and files with mode ``0o600`` — trace data is semi-sensitive.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from machina.observability.tracing import ActionTracer, TraceEntry

logger = structlog.get_logger(__name__)


class JSONLExporter:
    """Write trace entries as redacted JSONL with daily file rotation.

    Args:
        path: Directory where JSONL files are written.
        rotate_daily: If True (default), each day gets a separate file.
        max_summary_chars: Truncation limit for summary fields.

    Raises:
        FileNotFoundError: If ``path`` is inside a missing parent.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        rotate_daily: bool = True,
        max_summary_chars: int = 2000,
    ) -> None:
        self._dir = Path(path)
        self._rotate_daily = rotate_daily
        self._max_summary_chars = max_summary_chars
        self._lock = threading.Lock()

        self._dir.mkdir(parents=True, exist_ok=True)
        try:  # noqa: SIM105
            os.chmod(self._dir, 0o700)
        except OSError:
            pass

        logger.info("jsonl_exporter_ready", path=str(self._dir))

    def attach(self, tracer: ActionTracer) -> None:
        """Subscribe to a tracer so entries are written automatically."""
        tracer.subscribe(self._on_entry)

    def _on_entry(self, entry: TraceEntry) -> None:
        """Callback invoked by the tracer for each new entry."""
        line = entry.redacting_dump_json(max_summary_chars=self._max_summary_chars)
        filepath = self._current_filepath()
        with self._lock:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:  # noqa: SIM105
                os.chmod(filepath, 0o600)
            except OSError:
                pass

    def _current_filepath(self) -> Path:
        if self._rotate_daily:
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")
            return self._dir / f"traces-{date_str}.jsonl"
        return self._dir / "traces.jsonl"
