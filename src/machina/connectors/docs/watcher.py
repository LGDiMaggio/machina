"""File-system watcher — cross-cutting change detection for file-based connectors.

Uses watchdog's native observer when available (inotify/FSEvents/ReadDirectoryChanges),
falling back to PollingObserver for SMB/CIFS mounts where native events don't fire.
Debounces rapid save-then-reload patterns typical of Excel.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

import structlog

logger = structlog.get_logger(__name__)


class RefreshableConnector(Protocol):
    """Any connector with a synchronous refresh method."""

    def refresh(self) -> None: ...


def _require_watchdog() -> Any:
    try:
        import watchdog  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        from machina.exceptions import ConnectorError

        raise ConnectorError(
            "watchdog is required for file watching. Install with: pip install machina-ai[excel]"
        ) from exc
    return watchdog


class _DebouncedHandler:
    """watchdog event handler that coalesces rapid events into a single callback."""

    def __init__(
        self,
        paths: set[str],
        callback: Callable[[], None],
        debounce_sec: float,
    ) -> None:
        self._paths = {str(Path(p).resolve()) for p in paths}
        self._callback = callback
        self._debounce_sec = debounce_sec
        self._last_event: float = 0.0
        self._pending = False

    def dispatch(self, event: Any) -> None:
        if event.is_directory:
            return
        src = str(Path(event.src_path).resolve())
        if src not in self._paths:
            return
        now = time.monotonic()
        if now - self._last_event < self._debounce_sec:
            return
        self._last_event = now
        self._callback()


class FileWatcher:
    """Watches files for changes and triggers connector refresh.

    Args:
        paths: File paths to watch.
        callback: Called when a watched file changes.
        debounce_ms: Minimum interval between callback invocations.
        poll_fallback_sec: Polling interval for PollingObserver fallback.

    Example:
        ```python
        watcher = FileWatcher(
            paths=["/data/assets.xlsx"],
            callback=connector.refresh,
            debounce_ms=500,
        )
        await watcher.start()
        # ... later ...
        await watcher.stop()
        ```
    """

    def __init__(
        self,
        *,
        paths: list[str | Path],
        callback: Callable[[], None],
        debounce_ms: int = 500,
        poll_fallback_sec: int = 30,
    ) -> None:
        self._paths = [Path(p).resolve() for p in paths]
        self._callback = callback
        self._debounce_ms = debounce_ms
        self._poll_fallback_sec = poll_fallback_sec
        self._observer: Any = None
        self._running = False

    async def start(self) -> None:
        """Start watching in a background thread."""
        _require_watchdog()
        from watchdog.events import (  # type: ignore[import-not-found,unused-ignore]
            FileSystemEventHandler,
        )
        from watchdog.observers import Observer  # type: ignore[import-not-found,unused-ignore]
        from watchdog.observers.polling import (  # type: ignore[import-not-found,unused-ignore]
            PollingObserver,
        )

        path_strs = {str(p) for p in self._paths}
        handler = _DebouncedHandler(
            paths=path_strs,
            callback=self._callback,
            debounce_sec=self._debounce_ms / 1000.0,
        )

        # Wrap _DebouncedHandler so watchdog recognizes it
        class _WatchdogAdapter(FileSystemEventHandler):  # type: ignore[misc,unused-ignore]
            def on_any_event(self, event: Any) -> None:
                handler.dispatch(event)

        adapter = _WatchdogAdapter()

        # Collect unique parent directories to watch
        dirs = {str(p.parent) for p in self._paths}

        try:
            self._observer = Observer()
            for d in dirs:
                self._observer.schedule(adapter, d, recursive=False)
            await asyncio.to_thread(self._observer.start)
            self._running = True
            logger.info(
                "watcher_started",
                mode="native",
                paths=[str(p) for p in self._paths],
            )
        except Exception:
            logger.warning(
                "native_watcher_failed_falling_back_to_polling",
                poll_sec=self._poll_fallback_sec,
            )
            self._observer = PollingObserver(timeout=self._poll_fallback_sec)
            for d in dirs:
                self._observer.schedule(adapter, d, recursive=False)
            await asyncio.to_thread(self._observer.start)
            self._running = True
            logger.info(
                "watcher_started",
                mode="polling",
                poll_sec=self._poll_fallback_sec,
                paths=[str(p) for p in self._paths],
            )

    async def stop(self) -> None:
        """Stop the watcher."""
        if self._observer and self._running:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join, 5.0)
            self._running = False
            logger.info("watcher_stopped")

    @property
    def running(self) -> bool:
        return self._running
