#!/usr/bin/env python3
"""Build the sample asset registry spreadsheets from the JSON sources.

    python data/build_sample_registry.py

Writes ``asset_registry.xlsx`` (Italian names) and ``asset_registry_en.xlsx``
(English names) next to this script, from ``asset_registry.json`` and
``asset_registry_en.json``. The column headers are the ones ``config.yaml``
maps. Output is deterministic (fixed document timestamps), so re-running the
script on unchanged JSON reproduces the committed files.

Replace the spreadsheets with your own registry — any .xlsx or .csv works, as
long as ``config.yaml`` maps its columns.

Requires ``openpyxl`` (``pip install "machina-ai[excel]"``).
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

DATA_DIR = Path(__file__).resolve().parent
SHEET = "Asset"
# (column header, JSON key) — keep in sync with config.yaml.
COLUMNS = [
    ("Codice", "id"),
    ("Nome", "name"),
    ("Tipo", "type"),
    ("Ubicazione", "location"),
    ("Criticità", "criticality"),
    ("Costruttore", "manufacturer"),
    ("Modello", "model"),
    ("Modi di guasto", "failure_modes"),
    ("Alias", "aliases"),
]
FIXED_TIMESTAMP = datetime(2026, 1, 1)


def _cell(value: object) -> object:
    """Lists become the semicolon-delimited cells the connector splits."""
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return value


_DOC_TIMESTAMP = re.compile(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:)")


def _normalize_zip_timestamps(path: Path) -> None:
    """Rewrite the .xlsx (a zip) with fixed timestamps for stable bytes.

    openpyxl stamps the save time into ``docProps/core.xml`` and into every
    zip entry header; both are pinned here.
    """
    fixed = FIXED_TIMESTAMP.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    with zipfile.ZipFile(path) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info, payload in entries:
            if info.filename == "docProps/core.xml":
                payload = _DOC_TIMESTAMP.sub(rb"\g<1>" + fixed + rb"\g<2>", payload)
            stable = zipfile.ZipInfo(info.filename, date_time=FIXED_TIMESTAMP.timetuple()[:6])
            stable.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(stable, payload)


def build(source: Path, target: Path) -> int:
    """Write ``target`` from the JSON registry ``source``; return the row count."""
    assets = json.loads(source.read_text(encoding="utf-8"))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET
    sheet.append([header for header, _ in COLUMNS])
    for asset in assets:
        sheet.append([_cell(asset.get(key)) for _, key in COLUMNS])
    workbook.properties.created = FIXED_TIMESTAMP
    workbook.properties.modified = FIXED_TIMESTAMP
    workbook.save(target)
    _normalize_zip_timestamps(target)
    return len(assets)


def main() -> None:
    for source, target in (
        ("asset_registry.json", "asset_registry.xlsx"),
        ("asset_registry_en.json", "asset_registry_en.xlsx"),
    ):
        rows = build(DATA_DIR / source, DATA_DIR / target)
        print(f"wrote {target} ({rows} assets)")


if __name__ == "__main__":
    main()
