"""Write a constraints file's pins, without markers, as files pip-audit reads whole.

    python scripts/audit/pins.py deploy/docker/constraints.txt --out "$RUNNER_TEMP/pins"

``pip-audit -r FILE --no-deps --disable-pip`` audits the versions a file
pins without resolving or installing anything. On a universal constraints
file (``uv pip compile --universal``) it would still miss pins: it skips
every pin whose environment marker the runner does not match (soupsieve
2.10 is pinned for Python below 3.11.5 only), and it refuses a file that
pins one package twice, as such a file does on either side of a marker.

So each pin is written without its marker: ``layer-0.txt`` holds the first
version of every package, and each further version of a package goes into
the next layer. Auditing every layer audits every pinned version, whatever
platform or Python it is for. ``record.py security --constraints`` reads the
pins the same way (``record.read_pins``) and reports any it finds no audit
for. Exit code 0 when the layers are written, 2 when the file cannot be read
or holds a line that is not one exact pin; nothing is written then.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from record import read_pins


def write_layers(pins: dict[str, dict[str, str]], out: Path) -> list[Path]:
    """Write ``layer-N.txt`` files that name each package at most once."""
    out.mkdir(parents=True, exist_ok=True)
    by_name = {name: list(markers) for name, markers in sorted(pins.items())}
    layers = []
    for index in range(max(len(versions) for versions in by_name.values())):
        lines = [
            f"{name}=={versions[index]}\n"
            for name, versions in by_name.items()
            if index < len(versions)
        ]
        layer = out / f"layer-{index}.txt"
        layer.write_text("".join(lines), encoding="utf-8")
        layers.append(layer)
    return layers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("constraints", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="directory for layer-N.txt")
    args = parser.parse_args(argv)
    try:
        pins = read_pins(args.constraints)
    except (OSError, ValueError) as exc:
        print(f"{args.constraints}: {exc}", file=sys.stderr)
        return 2
    layers = write_layers(pins, args.out)
    total = sum(len(versions) for versions in pins.values())
    print(
        f"{args.constraints}: {total} pinned versions of {len(pins)} packages, {len(layers)} layers"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
