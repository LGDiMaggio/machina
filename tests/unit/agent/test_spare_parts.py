"""Tests for the bounded spare-part lookup result (``machina.agent.spare_parts``)."""

from __future__ import annotations

import json

import pytest

from machina.agent.spare_parts import SPARE_PARTS_RESULT_LIMIT, bounded_spare_parts
from machina.domain.spare_part import SparePart


def _inventory(size: int) -> list[SparePart]:
    return [SparePart(sku=f"PART-{i:05d}", name=f"Part {i}") for i in range(size)]


def _sku_only(part: SparePart) -> dict[str, str]:
    return {"sku": part.sku}


class TestBoundedSpareParts:
    """The result both spare-part lookup tools return."""

    def test_the_limit_is_fifty(self) -> None:
        assert SPARE_PARTS_RESULT_LIMIT == 50

    def test_a_lookup_within_the_limit_is_complete(self) -> None:
        result = bounded_spare_parts(_inventory(2), _sku_only)
        assert result == {
            "parts": [{"sku": "PART-00000"}, {"sku": "PART-00001"}],
            "total": 2,
            "truncated": False,
        }

    def test_no_parts_is_a_complete_empty_result(self) -> None:
        assert bounded_spare_parts([], _sku_only) == {"parts": [], "total": 0, "truncated": False}

    @pytest.mark.parametrize(
        ("size", "truncated"),
        [(SPARE_PARTS_RESULT_LIMIT, False), (SPARE_PARTS_RESULT_LIMIT + 1, True)],
    )
    def test_the_limit_itself_is_not_truncated(self, size: int, truncated: bool) -> None:
        result = bounded_spare_parts(_inventory(size), _sku_only)
        assert len(result["parts"]) == SPARE_PARTS_RESULT_LIMIT
        assert result["total"] == size
        assert result["truncated"] is truncated
        assert ("note" in result) is truncated

    def test_a_truncated_lookup_says_it_is_partial_and_how_to_narrow_it(self) -> None:
        result = bounded_spare_parts(_inventory(5_000), _sku_only)
        assert result["parts"] == [_sku_only(p) for p in _inventory(SPARE_PARTS_RESULT_LIMIT)]
        assert result["total"] == 5_000
        assert result["truncated"] is True
        assert result["note"] == (
            "Showing 50 of 5000 matching spare parts: the list is incomplete. Say so if "
            "you present it, and do not conclude that a part is missing or out of stock "
            "because it is not listed. Narrow the lookup, e.g. by sku, for a complete answer."
        )

    def test_the_note_carries_no_part_value(self) -> None:
        """The note is fixed text plus two counts: no SKU, name or other field."""
        parts = [
            SparePart(sku=f"SECRET-SKU-{i}", name=f"Hidden name {i}", warehouse_location="W9")
            for i in range(SPARE_PARTS_RESULT_LIMIT + 7)
        ]
        note = bounded_spare_parts(parts, _sku_only)["note"]
        for needle in ("SECRET-SKU", "Hidden name", "W9"):
            assert needle not in note

    def test_only_the_listed_parts_are_serialized(self) -> None:
        """The cost of a lookup scales with the limit, not with the inventory."""
        serialized: list[str] = []

        def serialize(part: SparePart) -> dict[str, str]:
            serialized.append(part.sku)
            return _sku_only(part)

        bounded_spare_parts(_inventory(5_000), serialize)
        assert serialized == [p.sku for p in _inventory(SPARE_PARTS_RESULT_LIMIT)]

    def test_the_connector_order_is_kept_and_the_input_left_alone(self) -> None:
        parts = list(reversed(_inventory(SPARE_PARTS_RESULT_LIMIT + 3)))
        before = list(parts)
        result = bounded_spare_parts(tuple(parts), _sku_only)
        assert [p["sku"] for p in result["parts"]] == [
            p.sku for p in before[:SPARE_PARTS_RESULT_LIMIT]
        ]
        assert parts == before

    def test_the_result_is_plain_json(self) -> None:
        result = bounded_spare_parts(_inventory(60), lambda p: p.model_dump(mode="json"))
        assert json.loads(json.dumps(result)) == result
