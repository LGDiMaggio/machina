"""Tests for generic_coercers.py — coercer functions and JSONPath-lite."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from machina.connectors.cmms.generic_coercers import (
    COERCER_REGISTRY,
    coerce_bool_truthy,
    coerce_enum_map,
    coerce_float,
    coerce_float_it,
    coerce_int,
    coerce_iso_date,
    coerce_iso_datetime,
    coerce_lowercase,
    coerce_regex_extract,
    coerce_strip_whitespace,
    resolve_path,
)
from machina.exceptions import ConnectorConfigError


class TestResolvePath:
    def test_simple_key(self) -> None:
        assert resolve_path({"a": 1}, "a") == 1

    def test_dotted(self) -> None:
        assert resolve_path({"a": {"b": {"c": 42}}}, "a.b.c") == 42

    def test_bracket_index(self) -> None:
        assert resolve_path({"items": [10, 20, 30]}, "items[1]") == 20

    def test_mixed(self) -> None:
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        assert resolve_path(data, "items[1].name") == "second"

    def test_missing_returns_none(self) -> None:
        assert resolve_path({"a": 1}, "b") is None

    def test_none_input(self) -> None:
        assert resolve_path(None, "a") is None

    def test_deep_missing(self) -> None:
        assert resolve_path({"a": {"b": 1}}, "a.c.d") is None

    def test_out_of_bounds_index(self) -> None:
        assert resolve_path({"items": [1]}, "items[5]") is None


class TestCoerceInt:
    def test_int(self) -> None:
        assert coerce_int(42) == 42

    def test_string(self) -> None:
        assert coerce_int("7") == 7

    def test_float_string(self) -> None:
        assert coerce_int("3.9") == 3


class TestCoerceFloat:
    def test_int_input(self) -> None:
        assert coerce_float(42) == 42.0

    def test_string(self) -> None:
        assert coerce_float("3.14") == 3.14


class TestCoerceFloatIt:
    def test_italian_comma(self) -> None:
        assert coerce_float_it("3,14") == 3.14

    def test_normal_dot(self) -> None:
        assert coerce_float_it("3.14") == 3.14

    def test_int_input(self) -> None:
        assert coerce_float_it(42) == 42.0


class TestCoerceBoolTruthy:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "sì", "si", "vero", "x"])
    def test_truthy(self, val: str) -> None:
        assert coerce_bool_truthy(val) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "falso", ""])
    def test_falsy(self, val: str) -> None:
        assert coerce_bool_truthy(val) is False

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            coerce_bool_truthy("maybe")


class TestCoerceIsoDate:
    def test_iso_string(self) -> None:
        assert coerce_iso_date("2024-04-16") == date(2024, 4, 16)

    def test_datetime_input(self) -> None:
        assert coerce_iso_date(datetime(2024, 1, 1, 12, 0)) == date(2024, 1, 1)

    def test_date_passthrough(self) -> None:
        d = date(2024, 4, 16)
        assert coerce_iso_date(d) == d


class TestCoerceIsoDatetime:
    def test_iso_string(self) -> None:
        result = coerce_iso_datetime("2024-04-16T14:30:00")
        assert result.year == 2024
        assert result.tzinfo is not None

    def test_naive_gets_utc(self) -> None:
        result = coerce_iso_datetime("2024-04-16")
        assert result.tzinfo is not None


class TestCoerceStripWhitespace:
    def test_strips(self) -> None:
        assert coerce_strip_whitespace("  hello  ") == "hello"


class TestCoerceLowercase:
    def test_lowercases(self) -> None:
        assert coerce_lowercase("  HELLO  ") == "hello"


class TestCoerceRegexExtract:
    def test_with_capture_group(self) -> None:
        result = coerce_regex_extract("WO-2026-001", pattern=r"WO-(\d{4})-")
        assert result == "2026"

    def test_no_match_returns_original(self) -> None:
        result = coerce_regex_extract("abc", pattern=r"(\d+)")
        assert result == "abc"

    def test_no_pattern_raises(self) -> None:
        with pytest.raises(ConnectorConfigError, match="pattern"):
            coerce_regex_extract("abc")


class TestCoerceEnumMap:
    def test_maps_value(self) -> None:
        result = coerce_enum_map("rot", enum_map={"rot": "rotating_equipment"})
        assert result == "rotating_equipment"

    def test_missing_with_default(self) -> None:
        result = coerce_enum_map("unknown", enum_map={"rot": "x"}, default="fallback")
        assert result == "fallback"

    def test_missing_no_default_raises(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            coerce_enum_map("unknown", enum_map={"rot": "x"})

    def test_no_enum_map_raises(self) -> None:
        with pytest.raises(ConnectorConfigError, match="enum_map"):
            coerce_enum_map("val")


class TestRegistry:
    def test_all_builtins_registered(self) -> None:
        expected = {
            "int",
            "float",
            "float_it",
            "bool_truthy",
            "iso_date",
            "iso_datetime",
            "strip_whitespace",
            "lowercase",
            "regex_extract",
            "enum_map",
        }
        assert expected == set(COERCER_REGISTRY.keys())
