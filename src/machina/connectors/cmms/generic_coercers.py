"""Named coercers for the GenericCmms YAML mapper.

Each coercer is a pure function that transforms a raw value from
an API response into the expected Python type.  The registry is
pluggable via ``machina.coercers`` entrypoints.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from machina.exceptions import ConnectorConfigError

# ------------------------------------------------------------------
# JSONPath-lite: dotted paths + bracket indexing
# ------------------------------------------------------------------


def resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dotted path like ``a.b.c`` or ``items[0].x`` from a nested dict/list."""
    parts = _split_path(path)
    current = obj
    for part in parts:
        if current is None:
            return None
        if isinstance(part, int):
            if isinstance(current, (list, tuple)) and 0 <= part < len(current):
                current = current[part]
            else:
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _split_path(path: str) -> list[str | int]:
    """Split ``a.b[0].c`` into ``['a', 'b', 0, 'c']``."""
    tokens: list[str | int] = []
    for m in _PATH_TOKEN_RE.finditer(path):
        if m.group(1) is not None:
            tokens.append(m.group(1))
        elif m.group(2) is not None:
            tokens.append(int(m.group(2)))
    return tokens


# ------------------------------------------------------------------
# Builtin coercers
# ------------------------------------------------------------------


def coerce_int(value: Any, **_: Any) -> int:
    """Coerce to integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(float(str(value)))


def coerce_float(value: Any, **_: Any) -> float:
    """Coerce to float."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(str(value))


def coerce_float_it(value: Any, **_: Any) -> float:
    """Coerce to float, handling Italian decimal comma."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    return float(s)


def coerce_bool_truthy(value: Any, **_: Any) -> bool:
    """Coerce to bool — understands 1/0, true/false, yes/no, sì/si."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "sì", "si", "vero", "x"):
        return True
    if s in ("0", "false", "no", "falso", ""):
        return False
    msg = f"Cannot coerce {value!r} to bool"
    raise ValueError(msg)


def coerce_iso_date(value: Any, **_: Any) -> date:
    """Parse an ISO 8601 date string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def coerce_iso_datetime(value: Any, **_: Any) -> datetime:
    """Parse an ISO 8601 datetime string, defaulting to UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def coerce_strip_whitespace(value: Any, **_: Any) -> str:
    """Strip leading/trailing whitespace."""
    return str(value).strip()


def coerce_lowercase(value: Any, **_: Any) -> str:
    """Lowercase the value."""
    return str(value).strip().lower()


def coerce_regex_extract(value: Any, *, pattern: str = "", **_: Any) -> str:
    """Extract a value using a regex with one capture group."""
    if not pattern:
        msg = "regex_extract requires a 'pattern' parameter"
        raise ConnectorConfigError(msg)
    m = re.search(pattern, str(value))
    if m and m.lastindex and m.lastindex >= 1:
        return m.group(1)
    return str(value)


def coerce_enum_map(
    value: Any, *, enum_map: dict[str, str] | None = None, default: Any = None, **_: Any
) -> Any:
    """Map a value through a lookup table."""
    if enum_map is None:
        msg = "enum_map coercer requires an 'enum_map' parameter"
        raise ConnectorConfigError(msg)
    key = str(value).strip()
    if key in enum_map:
        return enum_map[key]
    if default is not None:
        return default
    msg = f"Value {key!r} not found in enum_map {list(enum_map.keys())}"
    raise ValueError(msg)


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

COERCER_REGISTRY: dict[str, Any] = {
    "int": coerce_int,
    "float": coerce_float,
    "float_it": coerce_float_it,
    "bool_truthy": coerce_bool_truthy,
    "iso_date": coerce_iso_date,
    "iso_datetime": coerce_iso_datetime,
    "strip_whitespace": coerce_strip_whitespace,
    "lowercase": coerce_lowercase,
    "regex_extract": coerce_regex_extract,
    "enum_map": coerce_enum_map,
}


def _load_entrypoint_coercers() -> None:
    """Load user-registered coercers via ``machina.coercers`` entrypoints."""
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group: Any = (
            eps.get("machina.coercers", [])
            if isinstance(eps, dict)
            else eps.select(group="machina.coercers")
        )
        for ep in group:
            if ep.name not in COERCER_REGISTRY:
                COERCER_REGISTRY[ep.name] = ep.load()
    except Exception:
        pass


_load_entrypoint_coercers()
