"""Tests for the Plant domain entity."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from machina.domain.asset import Asset, AssetType
from machina.domain.plant import Plant
from machina.exceptions import AssetNotFoundError

if TYPE_CHECKING:
    from pathlib import Path


class TestPlant:
    """Test Plant asset registry."""

    def test_create_plant(self) -> None:
        plant = Plant(name="North Plant")
        assert plant.name == "North Plant"
        assert plant.assets == {}

    def test_register_and_get_asset(self, sample_asset: Asset) -> None:
        plant = Plant(name="Test Plant")
        plant.register_asset(sample_asset)
        retrieved = plant.get_asset("P-201")
        assert retrieved.id == "P-201"
        assert retrieved.name == sample_asset.name

    def test_get_missing_asset_raises(self) -> None:
        plant = Plant(name="Empty")
        with pytest.raises(AssetNotFoundError, match="MISSING"):
            plant.get_asset("MISSING")

    def test_list_assets(self, sample_asset: Asset) -> None:
        plant = Plant(name="Test Plant")
        plant.register_asset(sample_asset)
        motor = Asset(id="M-1", name="Motor", type=AssetType.ELECTRICAL)
        plant.register_asset(motor)
        assets = plant.list_assets()
        assert len(assets) == 2
        ids = {a.id for a in assets}
        assert ids == {"P-201", "M-1"}

    def test_list_assets_empty(self) -> None:
        plant = Plant(name="Empty")
        assert plant.list_assets() == []


class _FakeAssetConnector:
    """Stub connector exposing only read_assets()."""

    capabilities: ClassVar[list[str]] = ["read_assets"]

    def __init__(self, assets: list[Asset]) -> None:
        self._assets = assets

    async def read_assets(self, **kwargs: Any) -> list[Asset]:
        return list(self._assets)


def _flat_asset_dicts() -> list[dict[str, Any]]:
    """Flat list of asset dicts with parent references.

    Hierarchy:
        COOL-SYS (no parent)
        ├── P-201 (parent=COOL-SYS)
        └── P-202 (parent=COOL-SYS)
    """
    return [
        {
            "id": "COOL-SYS",
            "name": "Cooling System",
            "type": "static_equipment",
        },
        {
            "id": "P-201",
            "name": "Cooling Water Pump",
            "type": "rotating_equipment",
            "parent": "COOL-SYS",
            "equipment_class_code": "PU",
        },
        {
            "id": "P-202",
            "name": "Backup Cooling Pump",
            "type": "rotating_equipment",
            "parent": "COOL-SYS",
            "equipment_class_code": "PU",
        },
    ]


class TestLoadAssetsFrom:
    """Tests for Plant.load_assets_from() — MACHINA_SPEC R2."""

    @pytest.mark.asyncio
    async def test_load_from_connector(self) -> None:
        plant = Plant(name="Test")
        connector = _FakeAssetConnector(
            [
                Asset(id="A", name="A", type=AssetType.ROTATING_EQUIPMENT),
                Asset(id="B", name="B", type=AssetType.ELECTRICAL),
                Asset(id="C", name="C", type=AssetType.STATIC_EQUIPMENT),
            ]
        )
        n = await plant.load_assets_from(connector)
        assert n == 3
        assert {a.id for a in plant.list_assets()} == {"A", "B", "C"}

    @pytest.mark.asyncio
    async def test_load_from_json_file(self, tmp_path: Path) -> None:
        path = tmp_path / "assets.json"
        path.write_text(json.dumps(_flat_asset_dicts()), encoding="utf-8")
        plant = Plant(name="Test")
        n = await plant.load_assets_from(path)
        assert n == 3
        assert plant.get_asset("P-201").equipment_class_code == "PU"

    @pytest.mark.asyncio
    async def test_load_from_yaml_file(self, tmp_path: Path) -> None:
        yaml = pytest.importorskip("yaml")
        path = tmp_path / "assets.yaml"
        path.write_text(yaml.safe_dump(_flat_asset_dicts()), encoding="utf-8")
        plant = Plant(name="Test")
        n = await plant.load_assets_from(path)
        assert n == 3
        assert plant.get_asset("P-202").name == "Backup Cooling Pump"

    @pytest.mark.asyncio
    async def test_load_accepts_string_path(self, tmp_path: Path) -> None:
        path = tmp_path / "assets.json"
        path.write_text(json.dumps(_flat_asset_dicts()), encoding="utf-8")
        plant = Plant(name="Test")
        n = await plant.load_assets_from(str(path))
        assert n == 3

    @pytest.mark.asyncio
    async def test_load_reconstructs_hierarchy(self, tmp_path: Path) -> None:
        """R2 acceptance criteria: hierarchy is intact after load."""
        path = tmp_path / "assets.json"
        path.write_text(json.dumps(_flat_asset_dicts()), encoding="utf-8")
        plant = Plant(name="Test")
        await plant.load_assets_from(path)
        # COOL-SYS should have both pumps as children after reconstruction
        parent = plant.get_asset("COOL-SYS")
        assert sorted(parent.children) == ["P-201", "P-202"]
        # Children's parent pointer remains intact
        assert plant.get_asset("P-201").parent == "COOL-SYS"

    @pytest.mark.asyncio
    async def test_load_is_idempotent(self, tmp_path: Path) -> None:
        """Loading twice yields the same state, no duplicate children."""
        path = tmp_path / "assets.json"
        path.write_text(json.dumps(_flat_asset_dicts()), encoding="utf-8")
        plant = Plant(name="Test")
        await plant.load_assets_from(path)
        await plant.load_assets_from(path)
        assert len(plant.assets) == 3
        # children list should have exactly two entries, not four
        assert sorted(plant.get_asset("COOL-SYS").children) == ["P-201", "P-202"]

    @pytest.mark.asyncio
    async def test_load_dangling_parent_is_silent(self, tmp_path: Path) -> None:
        """An asset referencing a non-existent parent is still loaded,
        but its parent doesn't appear in the registry."""
        dangling = [
            {
                "id": "orphan",
                "name": "Orphan",
                "type": "rotating_equipment",
                "parent": "does-not-exist",
            },
        ]
        path = tmp_path / "assets.json"
        path.write_text(json.dumps(dangling), encoding="utf-8")
        plant = Plant(name="Test")
        n = await plant.load_assets_from(path)
        assert n == 1
        assert plant.get_asset("orphan").parent == "does-not-exist"

    @pytest.mark.asyncio
    async def test_load_from_invalid_source_raises_typeerror(self) -> None:
        plant = Plant(name="Test")
        with pytest.raises(TypeError, match="connector with read_assets"):
            await plant.load_assets_from(42)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_load_from_nonexistent_file_raises(self, tmp_path: Path) -> None:
        plant = Plant(name="Test")
        with pytest.raises(FileNotFoundError, match="not found"):
            await plant.load_assets_from(tmp_path / "missing.json")

    @pytest.mark.asyncio
    async def test_load_from_unsupported_format_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "assets.txt"
        path.write_text("anything", encoding="utf-8")
        plant = Plant(name="Test")
        with pytest.raises(ValueError, match="Unsupported asset file format"):
            await plant.load_assets_from(path)

    @pytest.mark.asyncio
    async def test_load_from_non_list_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "assets.json"
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        plant = Plant(name="Test")
        with pytest.raises(ValueError, match="must contain a list"):
            await plant.load_assets_from(path)
