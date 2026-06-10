"""File-system watcher — cross-cutting change detection for file-based connectors.

Uses watchdog's native observer when available (inotify/FSEvents/ReadDirectoryChanges),
falling back to PollingObserver for SMB/CIFS mounts where native events don't fire.
Debounces rapid save-then-reload patterns typical of Excel with a trailing-edge
timer: the callback fires once, after a full quiet period, so it always observes
the final on-disk state of a save burst.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

import structlog

logger = structlog.get_logger(__name__)


class RefreshableConnector(Protocol):
    """Any connector with a synchronous refresh method."""

    def refresh(self) -> None: ...


class _TimerLike(Protocol):
    """Minimal timer surface required by ``_DebouncedHandler`` (threading.Timer-shaped)."""

    def start(self) -> None: ...

    def cancel(self) -> None: ...


def _default_timer_factory(delay: float, fn: Callable[[], None]) -> _TimerLike:
    """Create a daemon ``threading.Timer`` so pending timers never block interpreter exit."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    return timer


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
    """watchdog event handler with trailing-edge debounce.

    Every matching event cancels any pending timer and arms a new one for
    ``debounce_sec``; the callback fires only when a timer expires with no
    further events — i.e. once per burst, after a full quiet period, observing
    the final on-disk state. Runs on the watchdog observer thread, so arm/cancel
    is guarded by a lock and a generation counter discards stale timers whose
    expiry races a cancel.

    Args:
        paths: Absolute file paths to react to.
        callback: Invoked once per coalesced burst, after the quiet period.
        debounce_sec: Quiet period in seconds. ``0.0`` still fires on a
            (zero-delay) timer, never synchronously from ``dispatch``.
        timer_factory: Injection point for tests — ``(delay, fn) -> timer``
            with ``start()``/``cancel()``. Defaults to a daemon
            ``threading.Timer``.
    """

    def __init__(
        self,
        paths: set[str],
        callback: Callable[[], None],
        debounce_sec: float,
        *,
        timer_factory: Callable[[float, Callable[[], None]], _TimerLike] | None = None,
    ) -> None:
        self._paths = {str(Path(p).resolve()) for p in paths}
        self._callback = callback
        self._debounce_sec = debounce_sec
        self._timer_factory = (
            timer_factory if timer_factory is not None else _default_timer_factory
        )
        self._lock = threading.Lock()
        self._timer: _TimerLike | None = None
        self._generation = 0
        # Set while no callback is executing; cleared for the duration of a
        # callback so ``drain`` can wait for an in-flight refresh to finish.
        self._idle = threading.Event()
        self._idle.set()

    def dispatch(self, event: Any) -> None:
        """Handle a watchdog event: (re)arm the trailing-edge debounce timer."""
        if event.is_directory:
            return
        src = str(Path(event.src_path).resolve())
        if src not in self._paths:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._generation += 1
            generation = self._generation
            timer = self._timer_factory(self._debounce_sec, lambda: self._fire(generation))
            self._timer = timer
            timer.start()

    def _fire(self, generation: int) -> None:
        """Timer expiry: invoke the callback unless this timer was superseded or cancelled.

        Runs on a ``threading.Timer`` thread, where an uncaught exception goes
        to ``threading.excepthook`` (stderr) — invisible in structured logs
        while the watcher silently keeps looking alive. The callback is
        therefore wrapped: failures are logged via structlog and swallowed, so
        the next burst still re-arms and fires.
        """
        with self._lock:
            if generation != self._generation or self._timer is None:
                return
            self._timer = None
            self._idle.clear()
        try:
            self._callback()
        except Exception as exc:
            logger.error(
                "debounce_callback_error",
                operation="debounce_callback",
                exc_type=type(exc).__name__,
                error=str(exc),
            )
        finally:
            self._idle.set()

    def cancel(self) -> None:
        """Cancel any pending timer so the callback can no longer fire."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def drain(self, timeout: float) -> bool:
        """Wait for an in-flight callback (if any) to finish.

        ``cancel`` only prevents PENDING timers from firing; a callback
        already executing cannot be cancelled. ``drain`` blocks until the
        handler is idle again (or immediately, when no callback is running).

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            ``True`` when the handler is idle, ``False`` on timeout (a
            callback is still executing).
        """
        return self._idle.wait(timeout)


class FileWatcher:
    """Watches files for changes and triggers connector refresh.

    Args:
        paths: File paths to watch.
        callback: Called once per change burst, after the debounce quiet period.
        debounce_ms: Trailing-edge quiet period — the callback fires this long
            after the *last* event of a burst.
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
        self._handler: _DebouncedHandler | None = None
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
        self._handler = handler

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
        """Stop the watcher, cancel pending timers, and drain an in-flight callback.

        After the observer thread is joined, the pending trailing-edge timer
        (if any) is cancelled so a PENDING callback can no longer fire. A
        callback already in flight cannot be cancelled: stop() waits for it
        (up to 5 seconds) and logs a warning if it is still running when the
        wait times out — in that case the refresh may complete after stop()
        returns.
        """
        if self._observer and self._running:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join, 5.0)
            self._running = False
            if self._handler is not None:
                self._handler.cancel()
                if not await asyncio.to_thread(self._handler.drain, 5.0):
                    logger.warning(
                        "watcher_callback_still_running",
                        operation="stop",
                        timeout_sec=5.0,
                    )
            logger.info("watcher_stopped")

    @property
    def running(self) -> bool:
        return self._running
