"""Tests for the shared dict→entity builders and the list-cell encoding.

``split_list_cell`` is the committed multi-value encoding shared by the
Excel and SQL substrates (semicolon-delimited string cells);
``dict_to_failure_mode`` and the ``failure_modes`` handling inside
``dict_to_asset`` are the shared mappers built on top of it.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from machina.connectors._entity_builders import (
    dict_to_asset,
    dict_to_failure_mode,
    split_list_cell,
)
from machina.domain.asset import AssetType, Criticality


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


class TestDictToAssetAliases:
    """The promotion trap: a key that used to survive in ``metadata``.

    Before ``aliases`` was an ``Asset`` field, an ``aliases`` column landed in
    ``metadata['aliases']`` via the catch-all. The catch-all excludes model
    fields, so adding the field WITHOUT an explicit pass-through would have
    deleted the data from both places at once — the field empty, the metadata
    key gone. These tests pin both halves.
    """

    def test_aliases_populated_and_absent_from_metadata(self) -> None:
        asset = dict_to_asset(
            {"id": "P-001", "name": "Cooling Water Pump", "aliases": "pompa acqua"}
        )
        assert asset.aliases == ["pompa acqua"]
        assert "aliases" not in asset.metadata

    def test_delimited_cell_splits_into_several_aliases(self) -> None:
        asset = dict_to_asset(
            {"id": "P-001", "name": "Cooling Water Pump", "aliases": "pompa acqua; bomba; CWP"}
        )
        assert asset.aliases == ["pompa acqua", "bomba", "CWP"]

    def test_list_valued_aliases_pass_through(self) -> None:
        asset = dict_to_asset(
            {"id": "P-001", "name": "Pompa 1", "aliases": ["  pompa vecchia ", ""]}
        )
        assert asset.aliases == ["pompa vecchia"]

    def test_missing_aliases_key_yields_empty_list_not_none(self) -> None:
        asset = dict_to_asset({"id": "P-001", "name": "Pompa 1"})
        assert asset.aliases == []

    def test_a_supplied_value_reaches_every_field_or_is_a_known_omission(self) -> None:
        """Guards the class of bug ``aliases`` nearly shipped as.

        A model field this builder never passes is unreachable from EVERY
        connector substrate, and unreachable silently — the catch-all excludes
        model fields, so the value does not even survive in ``metadata``. This
        supplies a distinctive value for every field and asserts it arrived,
        with an explicit allow-list for the two that legitimately do not.

        ``equipment_class_code`` is the live instance of exactly this bug
        (tracked separately, deliberately not fixed here). ``children`` is
        derived by ``Plant._rebuild_hierarchy``, not read from source data.
        """
        from machina.domain.asset import Asset

        known_omissions = {"children", "equipment_class_code"}
        supplied = {
            "id": "P-001",
            "name": "Pompa 1",
            "type": AssetType.STATIC_EQUIPMENT,
            "location": "Building A",
            "manufacturer": "ACME",
            "model": "M1",
            "serial_number": "SN1",
            "install_date": date(2020, 1, 1),
            "criticality": Criticality.A,
            "parent": "AREA-1",
            "failure_modes": "F-1",
            "aliases": "pompa vecchia",
            "metadata": {"ignored": True},
            "children": ["C-1"],
            "equipment_class_code": "PU",
        }
        assert set(supplied) == set(Asset.model_fields), (
            "Asset gained or lost a field — extend this dict so the omission "
            "check below still covers every field."
        )

        asset = dict_to_asset(supplied)
        defaults = Asset(id="X", name="X", type=AssetType.ROTATING_EQUIPMENT)
        unreached = {
            field
            for field in Asset.model_fields
            # ``metadata`` is intentionally rebuilt, not copied.
            if field != "metadata" and getattr(asset, field) == getattr(defaults, field)
        }
        assert unreached == known_omissions
