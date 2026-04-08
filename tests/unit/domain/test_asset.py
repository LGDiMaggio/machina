"""Tests for the Asset domain entity."""

from datetime import date

import pytest

from machina.domain.asset import Asset, AssetType, Criticality


class TestAsset:
    """Test Asset creation and behaviour."""

    def test_create_asset_with_required_fields(self) -> None:
        asset = Asset(id="P-201", name="Pump", type=AssetType.ROTATING_EQUIPMENT)
        assert asset.id == "P-201"
        assert asset.name == "Pump"
        assert asset.type == AssetType.ROTATING_EQUIPMENT

    def test_default_criticality_is_c(self) -> None:
        asset = Asset(id="X-1", name="Test", type=AssetType.INSTRUMENT)
        assert asset.criticality == Criticality.C

    def test_full_asset_fields(self, sample_asset: Asset) -> None:
        assert sample_asset.id == "P-201"
        assert sample_asset.manufacturer == "Grundfos"
        assert sample_asset.install_date == date(2019, 6, 15)
        assert sample_asset.criticality == Criticality.A
        assert sample_asset.parent == "COOLING-SYS-01"

    def test_asset_serialization_roundtrip(self, sample_asset: Asset) -> None:
        data = sample_asset.model_dump()
        restored = Asset.model_validate(data)
        assert restored.id == sample_asset.id
        assert restored.type == sample_asset.type
        assert restored.install_date == sample_asset.install_date

    def test_asset_children(self) -> None:
        asset = Asset(
            id="SYS-01",
            name="Cooling System",
            type=AssetType.STATIC_EQUIPMENT,
            children=["P-201", "P-202"],
        )
        assert len(asset.children) == 2
        assert "P-201" in asset.children

    def test_asset_metadata(self) -> None:
        asset = Asset(
            id="M-1",
            name="Motor",
            type=AssetType.ELECTRICAL,
            metadata={"power_kw": 75, "voltage": 400},
        )
        assert asset.metadata["power_kw"] == 75

    def test_asset_failure_modes(self) -> None:
        asset = Asset(
            id="P-201",
            name="Pump",
            type=AssetType.ROTATING_EQUIPMENT,
            failure_modes=["BEAR-WEAR-01", "SEAL-LEAK-01"],
        )
        assert len(asset.failure_modes) == 2

    def test_equipment_class_code_defaults_to_none(self) -> None:
        asset = Asset(id="P-201", name="Pump", type=AssetType.ROTATING_EQUIPMENT)
        assert asset.equipment_class_code is None

    def test_equipment_class_code_from_fixture(self, sample_asset: Asset) -> None:
        """The canonical fixture carries the ISO 14224 Table A.4 code."""
        assert sample_asset.equipment_class_code == "PU"

    def test_equipment_class_code_accepts_iso_code(self) -> None:
        asset = Asset(
            id="C-101",
            name="Main Compressor",
            type=AssetType.ROTATING_EQUIPMENT,
            equipment_class_code="CO",
        )
        assert asset.equipment_class_code == "CO"


class TestAssetType:
    """Test AssetType enum."""

    def test_all_types_exist(self) -> None:
        expected = {
            "rotating_equipment",
            "static_equipment",
            "instrument",
            "electrical",
            "piping",
            "structural",
            "hvac",
            "safety",
        }
        assert {t.value for t in AssetType} == expected


class TestCriticality:
    """Test Criticality enum."""

    def test_criticality_values(self) -> None:
        assert Criticality.A.value == "A"
        assert Criticality.B.value == "B"
        assert Criticality.C.value == "C"

    def test_invalid_criticality_rejected(self) -> None:
        with pytest.raises(ValueError):
            Asset(id="X", name="X", type=AssetType.INSTRUMENT, criticality="D")  # type: ignore[arg-type]

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id cannot be empty"):
            Asset(id="", name="Pump", type=AssetType.ROTATING_EQUIPMENT)

    def test_whitespace_only_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id cannot be empty"):
            Asset(id="   ", name="Pump", type=AssetType.ROTATING_EQUIPMENT)

    def test_id_stripped(self) -> None:
        asset = Asset(id="  P-201  ", name="Pump", type=AssetType.ROTATING_EQUIPMENT)
        assert asset.id == "P-201"
