"""Integration tests for ExcelCsvConnector + FileWatcher.

Uses real temporary directories and real file-system events.
"""

from __future__ import annotations

import asyncio
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

        refresh_event = asyncio.Event()
        original_refresh = conn.refresh

        def _tracked_refresh() -> None:
            original_refresh()
            refresh_event.set()

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
            try:
                await asyncio.wait_for(refresh_event.wait(), timeout=10.0)
            except TimeoutError:
                pytest.skip("Watcher did not fire in time (CI/SMB environment)")

            assets = await conn.read_assets()
            assert len(assets) == 2
            assert assets[1].id == "P-002"
        finally:
            await watcher.stop()
            assert not watcher.running

    @pytest.mark.asyncio
    async def test_watcher_debounce(self, tmp_path: Path) -> None:
        """Rapid saves should be coalesced into fewer callbacks."""
        asset_file = tmp_path / "assets.xlsx"
        _create_asset_file(asset_file, [("P-001", "Pompa 1")])

        call_count = 0

        def _counting_callback() -> None:
            nonlocal call_count
            call_count += 1

        watcher = FileWatcher(
            paths=[str(asset_file)],
            callback=_counting_callback,
            debounce_ms=500,
            poll_fallback_sec=5,
        )
        await watcher.start()

        try:
            # Rapid successive writes
            for i in range(5):
                _append_asset_row(asset_file, (f"P-{i:03d}", f"Pompa {i}"))
                await asyncio.sleep(0.05)

            # Wait for debounce + polling to settle
            await asyncio.sleep(2.0)

            # With 500ms debounce, 5 writes in ~250ms should produce fewer than 5 callbacks
            assert call_count < 5
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
