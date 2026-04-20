"""Tests for sql/dialect.py — EBCDIC, DB2 dates, Decimal, DSN redaction."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from machina.connectors.sql.dialect import (
    COERCER_REGISTRY,
    db2_date,
    iso_date,
    redact_dsn,
    strip_ebcdic,
    to_decimal,
    to_float,
    to_int,
    unix_ts,
)


class TestStripEbcdic:
    def test_bytes_cp037(self) -> None:
        raw = "PUMP-001".encode("cp037")
        assert strip_ebcdic(raw) == "PUMP-001"

    def test_bytes_with_padding(self) -> None:
        raw = "PUMP-001   ".encode("cp037")
        assert strip_ebcdic(raw) == "PUMP-001"

    def test_string_passthrough(self) -> None:
        assert strip_ebcdic("  hello  ") == "hello"

    def test_custom_codepage(self) -> None:
        raw = "TEST".encode("cp500")
        assert strip_ebcdic(raw, codepage="cp500") == "TEST"


class TestDb2Date:
    def test_century_date_2000s(self) -> None:
        assert db2_date("1240416") == date(2024, 4, 16)

    def test_century_date_1900s(self) -> None:
        assert db2_date("0990101") == date(1999, 1, 1)

    def test_iso_fallback(self) -> None:
        assert db2_date("2024-04-16") == date(2024, 4, 16)

    def test_datetime_input(self) -> None:
        dt = datetime(2024, 4, 16, 12, 0)
        assert db2_date(dt) == date(2024, 4, 16)

    def test_date_input(self) -> None:
        d = date(2024, 4, 16)
        assert db2_date(d) == d

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            db2_date("not-a-date")


class TestIsoDate:
    def test_iso_string(self) -> None:
        assert iso_date("2024-04-16") == date(2024, 4, 16)

    def test_datetime_input(self) -> None:
        assert iso_date(datetime(2024, 1, 1)) == date(2024, 1, 1)

    def test_date_passthrough(self) -> None:
        d = date(2024, 4, 16)
        assert iso_date(d) == d


class TestUnixTs:
    def test_integer_timestamp(self) -> None:
        result = unix_ts(1713264000)
        assert result.year == 2024
        assert result.tzinfo is not None

    def test_float_timestamp(self) -> None:
        result = unix_ts(1713264000.5)
        assert result.tzinfo == UTC

    def test_datetime_passthrough(self) -> None:
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        assert unix_ts(dt) is dt

    def test_naive_datetime_gets_utc(self) -> None:
        dt = datetime(2024, 1, 1)
        result = unix_ts(dt)
        assert result.tzinfo == UTC


class TestToDecimal:
    def test_decimal_passthrough(self) -> None:
        d = Decimal("1234.5678")
        assert to_decimal(d) == d

    def test_string(self) -> None:
        assert to_decimal("1234.5678") == Decimal("1234.5678")

    def test_int(self) -> None:
        assert to_decimal(42) == Decimal("42")

    def test_float_preserved_via_string(self) -> None:
        result = to_decimal("3.14")
        assert result == Decimal("3.14")

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot convert"):
            to_decimal("not-a-number")


class TestToFloat:
    def test_int(self) -> None:
        assert to_float(42) == 42.0

    def test_italian_comma(self) -> None:
        assert to_float("3,14") == 3.14

    def test_normal_dot(self) -> None:
        assert to_float("3.14") == 3.14


class TestToInt:
    def test_int_passthrough(self) -> None:
        assert to_int(42) == 42

    def test_string(self) -> None:
        assert to_int("7") == 7


class TestRedactDsn:
    def test_password_redacted(self) -> None:
        dsn = "Driver={ODBC};Server=host;PWD=secret123;UID=user"
        result = redact_dsn(dsn)
        assert "secret123" not in result
        assert "PWD=***" in result

    def test_mixed_case(self) -> None:
        dsn = "password=MyPass;uid=user"
        result = redact_dsn(dsn)
        assert "MyPass" not in result
        assert "password=***" in result

    def test_no_password(self) -> None:
        dsn = "Driver={ODBC};Server=host;UID=user"
        assert redact_dsn(dsn) == dsn

    def test_multiple_passwords(self) -> None:
        dsn = "PWD=pass1;Server=host;Password=pass2"
        result = redact_dsn(dsn)
        assert "pass1" not in result
        assert "pass2" not in result


class TestCoercerRegistry:
    def test_all_registered(self) -> None:
        expected = {
            "strip_ebcdic",
            "db2_date",
            "iso_date",
            "unix_ts",
            "decimal",
            "float",
            "int",
            "strip",
        }
        assert expected == set(COERCER_REGISTRY.keys())
