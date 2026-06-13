"""Tests for the shared dict→entity builders and the list-cell encoding.

``split_list_cell`` is the committed multi-value encoding shared by the
Excel and SQL substrates (semicolon-delimited string cells);
``dict_to_failure_mode`` and the ``failure_modes`` handling inside
``dict_to_asset`` are the shared mappers built on top of it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from machina.connectors._entity_builders import (
    dict_to_asset,
    dict_to_failure_mode,
    split_list_cell,
)


class TestSplitListCell:
    def test_basic_split(self) -> None:
        assert split_list_cell("BEAR-WEAR-01;SEAL-LEAK-01") == [
            "BEAR-WEAR-01",
            "SEAL-LEAK-01",
        ]

    def test_empty_string_returns_empty_list(self) -> None:
        assert split_list_cell("") == []

    def test_none_returns_empty_list(self) -> None:
        assert split_list_cell(None) == []

    def test_whitespace_around_entries_trimmed(self) -> None:
        assert split_list_cell("  BEAR-WEAR-01 ; SEAL-LEAK-01  ") == [
            "BEAR-WEAR-01",
            "SEAL-LEAK-01",
        ]

    def test_trailing_delimiter_tolerated(self) -> None:
        assert split_list_cell("BEAR-WEAR-01;SEAL-LEAK-01;") == [
            "BEAR-WEAR-01",
            "SEAL-LEAK-01",
        ]

    def test_only_delimiters_and_whitespace_returns_empty(self) -> None:
        assert split_list_cell(" ; ;; ") == []

    def test_existing_list_passed_through_cleaned(self) -> None:
        assert split_list_cell([" A ", "", "B"]) == ["A", "B"]


class TestDictToFailureMode:
    def test_basic(self) -> None:
        fm = dict_to_failure_mode(
            {
                "code": "BEAR-WEAR-01",
                "name": "Bearing wear",
                "category": "mechanical",
                "detection_methods": "vibration_analysis;thermography",
                "mtbf_hours": 8760,
            }
        )
        assert fm.code == "BEAR-WEAR-01"
        assert fm.name == "Bearing wear"
        assert fm.category == "mechanical"
        assert fm.detection_methods == ["vibration_analysis", "thermography"]
        assert fm.mtbf_hours == 8760.0

    def test_optional_fields_default(self) -> None:
        fm = dict_to_failure_mode({"code": "SEAL-LEAK-01", "name": "Seal leakage"})
        assert fm.mechanism == ""
        assert fm.detection_methods == []
        assert fm.mtbf_hours is None
        assert fm.iso_14224_code is None

    def test_numeric_string_mtbf_coerced_by_validation(self) -> None:
        fm = dict_to_failure_mode({"code": "X-01", "name": "X", "mtbf_hours": "26000"})
        assert fm.mtbf_hours == 26000.0

    def test_list_valued_fields_pass_through(self) -> None:
        fm = dict_to_failure_mode(
            {"code": "X-01", "name": "X", "typical_indicators": ["a", " b "]}
        )
        assert fm.typical_indicators == ["a", "b"]

    def test_null_code_raises_instead_of_minting_none_entity(self) -> None:
        """A NULL code column must hit the empty-code validator — never
        produce a literal 'None' catalog entry that dedups silently."""
        with pytest.raises(ValidationError):
            dict_to_failure_mode({"code": None, "name": None})


class TestDictToAssetFailureCodeLinkage:
    def test_failure_codes_resolved_from_delimited_string(self) -> None:
        asset = dict_to_asset(
            {"id": "P-001", "name": "Pompa 1", "failure_modes": "BEAR-WEAR-01;SEAL-LEAK-01"}
        )
        assert asset.failure_modes == ["BEAR-WEAR-01", "SEAL-LEAK-01"]

    def test_failure_codes_resolved_from_list(self) -> None:
        asset = dict_to_asset(
            {"id": "P-001", "name": "Pompa 1", "failure_modes": ["BEAR-WEAR-01"]}
        )
        assert asset.failure_modes == ["BEAR-WEAR-01"]

    def test_no_failure_codes_field(self) -> None:
        asset = dict_to_asset({"id": "P-001", "name": "Pompa 1"})
        assert asset.failure_modes == []

    def test_empty_codes_string(self) -> None:
        asset = dict_to_asset({"id": "P-001", "name": "Pompa 1", "failure_modes": ""})
        assert asset.failure_modes == []

    def test_failure_modes_not_duplicated_into_metadata(self) -> None:
        asset = dict_to_asset({"id": "P-001", "name": "Pompa 1", "failure_modes": "BEAR-WEAR-01"})
        assert "failure_modes" not in asset.metadata
