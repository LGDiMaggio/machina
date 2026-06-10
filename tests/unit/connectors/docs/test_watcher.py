"""Unit tests for ExcelCsvConnector file watcher (trailing-edge debounce)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


class FakeTimer:
    """Test double for threading.Timer — records arm/cancel, fired manually."""

    def __init__(self, delay: float, fn) -> None:
        self.delay = delay
        self._fn = fn
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        """Simulate timer expiry by invoking the armed function."""
        self._fn()


def _recording_factory(record: list[FakeTimer]):
    """Build a timer factory that appends every created FakeTimer to ``record``."""

    def factory(delay: float, fn) -> FakeTimer:
        timer = FakeTimer(delay, fn)
        record.append(timer)
        return timer

    return factory


def _file_event(path) -> MagicMock:
    event = MagicMock()
    event.is_directory = False
    event.src_path = str(path)
    return event


class TestFileWatcher:
    def test_import(self):
        from machina.connectors.docs.watcher import FileWatcher

        assert FileWatcher is not None

    def test_init_stores_paths_and_callback(self):
        from machina.connectors.docs.watcher import FileWatcher

        cb = MagicMock()
        watcher = FileWatcher(paths=["/tmp/test.xlsx"], callback=cb)
        assert watcher._paths == [Path("/tmp/test.xlsx").resolve()]
        assert watcher._callback is cb

    def test_debounce_default(self):
        from machina.connectors.docs.watcher import FileWatcher

        cb = MagicMock()
        watcher = FileWatcher(paths=["/tmp/test.xlsx"], callback=cb, debounce_ms=1000)
        assert watcher._debounce_ms == 1000

    def test_not_running_before_start(self):
        from machina.connectors.docs.watcher import FileWatcher

        cb = MagicMock()
        watcher = FileWatcher(paths=["/tmp/test.xlsx"], callback=cb)
        assert not watcher._running

    async def test_stop_cancels_pending_timer(self, tmp_path):
        """stop() must cancel a pending debounce timer — no callback after stop."""
        from machina.connectors.docs.watcher import FileWatcher, _DebouncedHandler

        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = _DebouncedHandler(
            paths={str(target)},
            callback=cb,
            debounce_sec=10.0,
            timer_factory=_recording_factory(timers),
        )
        handler.dispatch(_file_event(target))
        assert len(timers) == 1
        assert not timers[0].cancelled

        watcher = FileWatcher(paths=[str(target)], callback=cb)
        watcher._handler = handler
        watcher._observer = MagicMock()
        watcher._running = True

        await watcher.stop()

        assert timers[0].cancelled
        # Even if the timer expiry races the cancel, the callback must not fire.
        timers[0].fire()
        cb.assert_not_called()
        assert not watcher.running


class TestDebouncedHandler:
    """Trailing-edge debounce contract: the callback fires once, after a full
    quiet period, never on the first event of a burst."""

    def _handler(self, target, cb, timers, debounce_sec=0.5):
        from machina.connectors.docs.watcher import _DebouncedHandler

        return _DebouncedHandler(
            paths={str(target)},
            callback=cb,
            debounce_sec=debounce_sec,
            timer_factory=_recording_factory(timers),
        )

    def test_single_event_fires_once_after_quiet_period(self, tmp_path):
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers, debounce_sec=0.5)

        handler.dispatch(_file_event(target))

        # Trailing edge: nothing fires at dispatch time.
        cb.assert_not_called()
        assert len(timers) == 1
        assert timers[0].started
        assert timers[0].delay == 0.5

        timers[0].fire()
        cb.assert_called_once()

    def test_rapid_events_rearm_and_fire_once(self, tmp_path):
        """Two events inside the window: first timer cancelled, second fires (AE1)."""
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers)

        handler.dispatch(_file_event(target))
        handler.dispatch(_file_event(target))

        cb.assert_not_called()
        assert len(timers) == 2
        assert timers[0].cancelled
        assert timers[1].started
        assert not timers[1].cancelled

        timers[1].fire()
        cb.assert_called_once()

    def test_ignores_non_matching_path(self, tmp_path):
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers)

        handler.dispatch(_file_event(tmp_path / "other.xlsx"))

        assert timers == []
        cb.assert_not_called()

    def test_ignores_directory_events(self, tmp_path):
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers)

        event = _file_event(target)
        event.is_directory = True
        handler.dispatch(event)

        assert timers == []
        cb.assert_not_called()

    def test_superseded_timer_racing_expiry_does_not_fire(self, tmp_path):
        """A cancelled timer whose function already started must not produce a
        second callback — only the newest timer may fire (lock-protected)."""
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers)

        handler.dispatch(_file_event(target))
        handler.dispatch(_file_event(target))  # supersedes timers[0]

        # Simulate cancel() losing the race: the stale timer fires anyway.
        timers[0].fire()
        cb.assert_not_called()

        timers[1].fire()
        cb.assert_called_once()

    def test_cancel_prevents_pending_callback(self, tmp_path):
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers)

        handler.dispatch(_file_event(target))
        handler.cancel()

        assert timers[0].cancelled
        # Even if expiry already started when cancel() ran, no callback.
        timers[0].fire()
        cb.assert_not_called()

    def test_second_burst_after_callback_fires_again(self, tmp_path):
        """Debounce resets after a completed callback — not one-shot."""
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers)

        handler.dispatch(_file_event(target))
        timers[0].fire()
        assert cb.call_count == 1

        handler.dispatch(_file_event(target))
        assert len(timers) == 2
        assert timers[1].started
        timers[1].fire()
        assert cb.call_count == 2

    def test_zero_debounce_is_still_trailing_edge(self, tmp_path):
        """debounce_sec=0.0 arms a zero-delay timer — never fires synchronously."""
        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        timers: list[FakeTimer] = []
        handler = self._handler(target, cb, timers, debounce_sec=0.0)

        handler.dispatch(_file_event(target))

        cb.assert_not_called()
        assert len(timers) == 1
        assert timers[0].delay == 0.0

        timers[0].fire()
        cb.assert_called_once()
