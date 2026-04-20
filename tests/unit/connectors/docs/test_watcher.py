"""Unit tests for ExcelCsvConnector file watcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


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


class TestDebouncedHandler:
    def test_dispatches_matching_path(self, tmp_path):
        from machina.connectors.docs.watcher import _DebouncedHandler

        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        handler = _DebouncedHandler(paths={str(target)}, callback=cb, debounce_sec=0.0)

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(target)
        handler.dispatch(event)
        cb.assert_called_once()

    def test_ignores_non_matching_path(self, tmp_path):
        from machina.connectors.docs.watcher import _DebouncedHandler

        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        handler = _DebouncedHandler(paths={str(target)}, callback=cb, debounce_sec=0.0)

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(tmp_path / "other.xlsx")
        handler.dispatch(event)
        cb.assert_not_called()

    def test_debounce_suppresses_rapid_events(self, tmp_path):
        from machina.connectors.docs.watcher import _DebouncedHandler

        target = tmp_path / "test.xlsx"
        target.touch()
        cb = MagicMock()
        handler = _DebouncedHandler(paths={str(target)}, callback=cb, debounce_sec=10.0)

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(target)
        handler.dispatch(event)
        handler.dispatch(event)
        handler.dispatch(event)
        cb.assert_called_once()
