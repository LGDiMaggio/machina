"""SQL dialect layer — EBCDIC, DB2 dates, Decimal preservation, DSN redaction.

Named coercers referenced from YAML schemas. Pure functions, no I/O.
"""

from __future__ import annotations

import codecs
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# ------------------------------------------------------------------
# EBCDIC
# ------------------------------------------------------------------


def strip_ebcdic(value: Any, *, codepage: str = "cp037") -> str:
    """Decode an EBCDIC-encoded bytes value to a stripped Python string."""
    if isinstance(value, bytes):
        return codecs.decode(value, codepage).strip()
    return str(value).strip()


# ------------------------------------------------------------------
# Date coercers
# ------------------------------------------------------------------

_DB2_CENTURY_RE = re.compile(r"^(\d)(\d{2})(\d{2})(\d{2})$")


def db2_date(value: Any) -> date:
    """Parse DB2 7-digit century-date format (e.g. 1240416 → 2024-04-16).

    Format: CYYMMDD where C=0 means 1900s, C=1 means 2000s.
    Also accepts ISO strings and datetime objects as fallback.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = _DB2_CENTURY_RE.match(s)
    if m:
        century = 1900 + int(m.group(1)) * 100
        year = century + int(m.group(2))
        month = int(m.group(3))
        day = int(m.group(4))
        return date(year, month, day)
    return date.fromisoformat(s)


def iso_date(value: Any) -> date:
    """Parse an ISO 8601 date string (YYYY-MM-DD)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def unix_ts(value: Any) -> datetime:
    """Convert a Unix timestamp (seconds since epoch) to a UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return datetime.fromtimestamp(float(value), tz=UTC)


# ------------------------------------------------------------------
# Numeric
# ------------------------------------------------------------------


def to_decimal(value: Any) -> Decimal:
    """Preserve exact decimal precision (DB2 DECIMAL, SQL Server money)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        msg = f"Cannot convert {value!r} to Decimal"
        raise ValueError(msg) from exc


def to_float(value: Any) -> float:
    """Parse a float, handling Italian decimal comma."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    return float(s)


def to_int(value: Any) -> int:
    """Parse an integer."""
    if isinstance(value, int):
        return value
    return int(to_float(value))


# ------------------------------------------------------------------
# Coercer registry
# ------------------------------------------------------------------

COERCER_REGISTRY: dict[str, Any] = {
    "strip_ebcdic": strip_ebcdic,
    "db2_date": db2_date,
    "iso_date": iso_date,
    "unix_ts": unix_ts,
    "decimal": to_decimal,
    "float": to_float,
    "int": to_int,
    "strip": lambda v: str(v).strip(),
}


# ------------------------------------------------------------------
# DSN redaction
# ------------------------------------------------------------------

_DSN_PASSWORD_RE = re.compile(
    r"((?:PWD|Password|pwd)\s*=\s*)([^;]+)",
    re.IGNORECASE,
)


def redact_dsn(dsn: str) -> str:
    """Replace password values in a DSN/connection string with ***."""
    return _DSN_PASSWORD_RE.sub(r"\1***", dsn)
