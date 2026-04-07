"""Tests for the Plant domain entity."""

import pytest

from machina.domain.asset import Asset, AssetType
from machina.domain.plant import Plant
from machina.exceptions import AssetNotFoundError


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
