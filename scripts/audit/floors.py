"""Pin every core dependency to its declared floor, as a pip constraints file.

    python scripts/audit/floors.py pyproject.toml > floors.txt
    pip install -c floors.txt -e ".[dev]"

Each ``name>=X`` in ``[project].dependencies`` becomes ``name==X``, with
extras and markers dropped (constraints files take neither). A dependency
without a ``>=`` bound is an error naming it, never skipped: a skipped entry
would resolve to the newest release while the leg still reports "floors".
"""

from __future__ import annotations

import re
import sys
import tomllib

_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
_FLOOR = re.compile(r">=\s*([^\s,]+)")


def floor_pins(dependencies: list[str]) -> list[str]:
    """Return ``name==floor`` for each dependency, in declaration order."""
    pins, unbounded = [], []
    for dependency in dependencies:
        match = _NAME.match(dependency)
        floor = _FLOOR.search(match[2]) if match else None
        if match is None or floor is None:
            unbounded.append(dependency)
        else:
            pins.append(f"{match[1]}=={floor[1]}")
    if unbounded:
        raise SystemExit(f"core dependencies without a >= floor: {', '.join(unbounded)}")
    return pins


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "pyproject.toml"
    with open(path, "rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    print("\n".join(floor_pins(dependencies)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
