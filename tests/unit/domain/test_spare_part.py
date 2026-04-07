"""Tests for the SparePart domain entity."""

from machina.domain.spare_part import SparePart


class TestSparePart:
    """Test SparePart creation and inventory logic."""

    def test_create_spare_part(self, sample_spare_part: SparePart) -> None:
        assert sample_spare_part.sku == "SKF-6310"
        assert sample_spare_part.stock_quantity == 4
        assert sample_spare_part.unit_cost == 45.00

    def test_needs_reorder_false(self, sample_spare_part: SparePart) -> None:
        # stock=4, reorder_point=2 → no reorder needed
        assert sample_spare_part.needs_reorder is False

    def test_needs_reorder_true_at_threshold(self) -> None:
        part = SparePart(sku="X", name="X", stock_quantity=2, reorder_point=2)
        assert part.needs_reorder is True

    def test_needs_reorder_true_below_threshold(self) -> None:
        part = SparePart(sku="X", name="X", stock_quantity=1, reorder_point=3)
        assert part.needs_reorder is True

    def test_compatible_assets(self, sample_spare_part: SparePart) -> None:
        assert "P-201" in sample_spare_part.compatible_assets
        assert len(sample_spare_part.compatible_assets) == 3

    def test_serialization_roundtrip(self, sample_spare_part: SparePart) -> None:
        data = sample_spare_part.model_dump()
        restored = SparePart.model_validate(data)
        assert restored.sku == sample_spare_part.sku
        assert restored.needs_reorder == sample_spare_part.needs_reorder
