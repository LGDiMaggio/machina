"""Tests for generic_schema.py and the YAML mapper engine in generic.py."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from machina.connectors.cmms.generic import (
    _yaml_map_row,
    _yaml_reverse_row,
)
from machina.connectors.cmms.generic_schema import (
    EndpointSpec,
    EntityMapping,
    FieldSpec,
    GenericCmmsYamlConfig,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "generic_cmms"


# ------------------------------------------------------------------
# Schema validation tests
# ------------------------------------------------------------------


class TestFieldSpec:
    def test_valid_basic(self) -> None:
        f = FieldSpec(source="machine_code")
        assert f.coerce is None
        assert f.required is False

    def test_valid_with_coerce(self) -> None:
        f = FieldSpec(source="name", coerce="strip_whitespace")
        assert f.coerce == "strip_whitespace"

    def test_unknown_coercer_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown coercer"):
            FieldSpec(source="x", coerce="nonexistent_coercer")

    def test_regex_extract_requires_pattern(self) -> None:
        with pytest.raises(ValueError, match="pattern"):
            FieldSpec(source="x", coerce="regex_extract")

    def test_regex_extract_with_pattern(self) -> None:
        f = FieldSpec(source="x", coerce="regex_extract", pattern=r"(\d+)")
        assert f.pattern == r"(\d+)"

    def test_enum_map_requires_map(self) -> None:
        with pytest.raises(ValueError, match="enum_map"):
            FieldSpec(source="x", coerce="enum_map")

    def test_enum_map_valid(self) -> None:
        f = FieldSpec(source="x", coerce="enum_map", enum_map={"a": "b"})
        assert f.enum_map == {"a": "b"}


class TestGenericCmmsYamlConfig:
    def test_empty_mapping_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            GenericCmmsYamlConfig(mapping={})

    def test_unknown_entity_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown entity type"):
            GenericCmmsYamlConfig(
                mapping={
                    "invalid_type": EntityMapping(
                        endpoint=EndpointSpec(path="/x"),
                        fields={"id": FieldSpec(source="x")},
                    )
                }
            )

    def test_valid_config(self) -> None:
        cfg = GenericCmmsYamlConfig(
            mapping={
                "asset": EntityMapping(
                    endpoint=EndpointSpec(path="/machines"),
                    fields={"id": FieldSpec(source="machine_code")},
                ),
            }
        )
        assert "asset" in cfg.mapping


class TestSampleConfig:
    def test_sample_yaml_parses(self) -> None:
        raw = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text())
        cfg = GenericCmmsYamlConfig.model_validate(raw)
        assert "asset" in cfg.mapping
        assert "work_order" in cfg.mapping
        assert cfg.mapping["asset"].root == "items"
        assert len(cfg.mapping["asset"].fields) >= 5


# ------------------------------------------------------------------
# YAML mapper engine tests
# ------------------------------------------------------------------


def _asset_mapping() -> EntityMapping:
    raw = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text())
    cfg = GenericCmmsYamlConfig.model_validate(raw)
    return cfg.mapping["asset"]


def _wo_mapping() -> EntityMapping:
    raw = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text())
    cfg = GenericCmmsYamlConfig.model_validate(raw)
    return cfg.mapping["work_order"]


def _sample_items() -> list[dict[str, Any]]:
    raw = json.loads((FIXTURES / "sample_payload.json").read_text())
    return raw["items"]


class TestYamlMapRow:
    def test_happy_path_asset(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        result = _yaml_map_row(mapping, items[0])
        assert result["id"] == "P-001"
        assert result["name"] == "Pompa centrifuga linea 1"
        assert result["type"] == "rotating_equipment"
        assert result["criticality"] == "A"
        assert result["install_date"] == date(2019, 3, 15)

    def test_nested_metadata(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        result = _yaml_map_row(mapping, items[0])
        assert result["metadata"]["manufacturer"] == "Grundfos"
        assert result["metadata"]["model"] == "CR 32-2"

    def test_all_items_map(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        results = [_yaml_map_row(mapping, item) for item in items]
        assert len(results) == 10
        assert all(r.get("id") for r in results)

    def test_enum_map_static_equipment(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        # V-001 has category "sta" → static_equipment
        result = _yaml_map_row(mapping, items[2])
        assert result["type"] == "static_equipment"

    def test_null_parent_uses_default(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        result = _yaml_map_row(mapping, items[0])
        assert result["parent"] is None

    def test_parent_populated(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        # M-001 has parent_machine_code "N-001"
        result = _yaml_map_row(mapping, items[3])
        assert result["parent"] == "N-001"

    def test_strip_whitespace(self) -> None:
        mapping = _asset_mapping()
        items = _sample_items()
        result = _yaml_map_row(mapping, items[0])
        # machine_name has leading/trailing spaces in fixture
        assert result["name"] == "Pompa centrifuga linea 1"
        assert result["metadata"]["manufacturer"] == "Grundfos"

    def test_missing_source_with_default(self) -> None:
        mapping = EntityMapping(
            endpoint=EndpointSpec(path="/x"),
            fields={
                "id": FieldSpec(source="code"),
                "name": FieldSpec(source="missing_field", default="(unnamed)"),
            },
        )
        result = _yaml_map_row(mapping, {"code": "X-001"})
        assert result["name"] == "(unnamed)"

    def test_required_field_missing_skips_row(self) -> None:
        mapping = EntityMapping(
            endpoint=EndpointSpec(path="/x"),
            fields={
                "id": FieldSpec(source="code", required=True),
                "name": FieldSpec(source="name"),
            },
        )
        result = _yaml_map_row(mapping, {"name": "No code"})
        assert result == {}

    def test_work_order_mapping(self) -> None:
        mapping = _wo_mapping()
        raw = {
            "wo_id": "WO-001",
            "machine_code": "P-001",
            "descr": "Fix pump",
            "priority_code": "1",
        }
        result = _yaml_map_row(mapping, raw)
        assert result["id"] == "WO-001"
        assert result["asset_id"] == "P-001"
        assert result["priority"] == "high"


class TestYamlReverseRow:
    def test_simple_reverse(self) -> None:
        mapping = _wo_mapping()
        domain = {"description": "Fix pump", "priority": "high", "asset_id": "P-001"}
        result = _yaml_reverse_row(mapping, domain)
        assert result["descr"] == "Fix pump"
        assert result["priority_code"] == "1"
        assert result["machine_code"] == "P-001"

    def test_reverse_enum_map_missing_uses_original(self) -> None:
        mapping = _wo_mapping()
        domain = {"description": "Test", "priority": "emergency", "asset_id": "X"}
        result = _yaml_reverse_row(mapping, domain)
        assert result["priority_code"] == "emergency"

    def test_reverse_with_none_value(self) -> None:
        mapping = _wo_mapping()
        domain = {"description": None, "priority": None, "asset_id": "X"}
        result = _yaml_reverse_row(mapping, domain)
        assert result["descr"] is None
