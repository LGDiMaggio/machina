"""Pin each core dependency to the oldest release its declaration admits.

    python -m pip install uv==<version pinned in audit.yml>
    python scripts/audit/floors.py --out floors.txt --facts floors.json
    pip install -c floors.txt -e ".[dev]"

A lower bound is not always a release: ``structlog>=24.0`` is valid, but
structlog has no 24.0, so pinning the literal bound (``==24.0``) makes pip
fail before a single test runs. Instead, uv resolves ``[project].dependencies``
with ``--resolution lowest-direct``: each direct dependency gets the oldest
release that exists, satisfies every specifier and supports this interpreter.
Only those direct pins go into the constraints file, so transitive and dev
dependencies resolve freely. The facts file records each floor and every
place where it differs from the declared bound.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
_LOWER_BOUND = re.compile(r">=\s*([^\s,]+)")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def release(version: str) -> tuple[int, ...]:
    """``24.0`` -> ``(24,)``: compares release numbers, ignoring trailing zeros."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    parts = [int(part) for part in match[0].split(".")] if match else []
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def resolve_lowest(pyproject: Path) -> dict[str, str]:
    """Every package uv resolves for the core dependencies, at lowest-direct."""
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is not installed: python -m pip install uv")
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    command = [uv, "pip", "compile", str(pyproject), "--resolution", "lowest-direct"]
    command += ["--python-version", python, "--no-header", "--no-annotate", "--quiet"]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"uv could not resolve the floors:\n{proc.stderr}")
    pins = {}
    for line in proc.stdout.splitlines():
        name, separator, version = line.partition("==")
        if separator:
            pins[canonical(name)] = version.split(";")[0].strip()
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--out", type=Path, required=True, help="constraints file")
    parser.add_argument("--facts", type=Path, required=True, help="JSON facts for record.py")
    args = parser.parse_args(argv)

    with args.pyproject.open("rb") as handle:
        declared = tomllib.load(handle)["project"]["dependencies"]
    resolved = resolve_lowest(args.pyproject)
    floors, notes = {}, []
    for requirement in declared:
        match = _REQUIREMENT.match(requirement)
        if match is None or canonical(match[1]) not in resolved:
            raise SystemExit(f"uv resolved no version for {requirement!r}")
        name, version = canonical(match[1]), resolved[canonical(match[1])]
        floors[name] = version
        bound = _LOWER_BOUND.search(match[2])
        if bound is None:
            notes.append(f"{name}: no lower bound declared; oldest release is {version}")
        elif release(bound[1]) != release(version):
            notes.append(f"{name}: declared >={bound[1]}, oldest installable is {version}")
    args.out.write_text("".join(f"{n}=={v}\n" for n, v in floors.items()), encoding="utf-8")
    facts = {"floors": floors, "floor_notes": notes}
    args.facts.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(facts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
