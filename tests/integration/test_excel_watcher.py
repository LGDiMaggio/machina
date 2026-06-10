"""Integration tests for ExcelCsvConnector + FileWatcher.

Uses real temporary directories and real file-system events.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

openpyxl = pytest.importorskip("openpyxl")

if TYPE_CHECKING:
    from pathlib import Path

from machina.connectors.docs.excel import ExcelCsvConnector  # noqa: E402
from machina.connectors.docs.excel_schema import (  # noqa: E402
    ColumnMapping,
    ExcelConnectorConfig,
    SheetSchema,
    WatcherConfig,
)
from machina.connectors.docs.watcher import FileWatcher  # noqa: E402


def _create_asset_file(path: Path, rows: list[tuple[str, str]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Assets"
    ws.append(["Codice", "Nome"])
    for row in rows:
        ws.append(list(row))
    wb.save(str(path))
    wb.close()


def _append_asset_row(path: Path, row: tuple[str, str]) -> None:
    wb = openpyxl.load_workbook(str(path))
    ws = wb["Assets"]
    ws.append(list(row))
    wb.save(str(path))
    wb.close()


def _count_asset_rows(path: Path) -> int:
    """Return the number of rows (including header) currently on disk."""
    wb = openpyxl.load_workbook(str(path), read_only=True)
    try:
        return int(wb["Assets"].max_row)
    finally:
        wb.close()


def _asset_schema(path: Path) -> SheetSchema:
    return SheetSchema(
        path=str(path),
        sheet="Assets",
        columns=[
            ColumnMapping(column="Codice", field="id", required=True),
            ColumnMapping(column="Nome", field="name", required=True),
        ],
    )


class TestWatcherIntegration:
    @pytest.mark.asyncio
    async def test_watcher_detects_change_and_refreshes(self, tmp_path: Path) -> None:
        asset_file = tmp_path / "assets.xlsx"
        _create_asset_file(asset_file, [("P-001", "Pompa 1")])

        config = ExcelConnectorConfig(
            asset_registry=_asset_schema(asset_file),
            watcher=WatcherConfig(debounce_ms=100, poll_fallback_sec=5),
        )
        conn = ExcelCsvConnector(config=config)
        await conn.connect()
        assert len(await conn.read_assets()) == 1

        # The callback runs on the debounce timer thread — use a threading.Event.
        refresh_done = threading.Event()
        original_refresh = conn.refresh

        def _tracked_refresh() -> None:
            original_refresh()
            refresh_done.set()

        watcher = FileWatcher(
            paths=[str(asset_file)],
            callback=_tracked_refresh,
            debounce_ms=100,
            poll_fallback_sec=5,
        )
        await watcher.start()
        assert watcher.running

        try:
            # Modify file externally
            _append_asset_row(asset_file, ("P-002", "Pompa 2"))

            # Wait for the watcher to fire (up to 10s for polling fallback)
            if not await asyncio.to_thread(refresh_done.wait, 10.0):
                pytest.fail(
                    "Watcher did not fire within 10s after the file changed — "
                    "the observer or the trailing-edge debounce timer is broken"
                )

            assets = await conn.read_assets()
            assert len(assets) == 2
            assert assets[1].id == "P-002"
        finally:
            await watcher.stop()
            assert not watcher.running

    @pytest.mark.asyncio
    async def test_watcher_debounce(self, tmp_path: Path) -> None:
        """A rapid save burst coalesces into exactly one trailing-edge callback
        that observes the final on-disk state."""
        asset_file = tmp_path / "assets.xlsx"
        _create_asset_file(asset_file, [("P-001", "Pompa 1")])

        observed_rows: list[int] = []
        fired = threading.Event()

        def _recording_callback() -> None:
            observed_rows.append(_count_asset_rows(asset_file))
            fired.set()

        watcher = FileWatcher(
            paths=[str(asset_file)],
            callback=_recording_callback,
            debounce_ms=500,
            poll_fallback_sec=5,
        )
        await watcher.start()

        try:
            # Rapid successive writes, all inside the 500 ms debounce window
            for i in range(5):
                _append_asset_row(asset_file, (f"P-{i:03d}", f"Pompa {i}"))
                await asyncio.sleep(0.05)

            if not await asyncio.to_thread(fired.wait, 10.0):
                pytest.fail(
                    "Watcher did not fire within 10s after the save burst — "
                    "the observer or the trailing-edge debounce timer is broken"
                )

            # Let a full extra quiet period elapse: no further callback may arrive.
            await asyncio.sleep(1.5)

            # Trailing edge: exactly one callback, observing the final on-disk
            # state (header + 1 initial row + 5 appended rows).
            assert observed_rows == [7]
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_watcher_start_stop(self, tmp_path: Path) -> None:
        asset_file = tmp_path / "assets.xlsx"
        _create_asset_file(asset_file, [("P-001", "Pompa 1")])

        watcher = FileWatcher(
            paths=[str(asset_file)],
            callback=lambda: None,
        )
        assert not watcher.running
        await watcher.start()
        assert watcher.running
        await watcher.stop()
        assert not watcher.running
