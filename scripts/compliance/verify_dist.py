"""Check the LICENSE and licence metadata of a built wheel and sdist.

    python -m build
    python scripts/compliance/verify_dist.py --dist dist --license LICENSE \
        --report evidence/verify-dist.json

``--dist`` must hold exactly one wheel and one sdist. Both must carry a LICENSE
byte-identical to ``--license``: the wheel as ``<name>.dist-info/licenses/LICENSE``,
the sdist at the root of its one top-level directory. The wheel's METADATA and
the sdist's PKG-INFO must declare ``License-Expression: Apache-2.0`` and
``License-File: LICENSE``. The wheel may hold nothing at its top level but
``--package`` and its own dist-info, so third-party code cannot be added as a
separate top-level package or module unnoticed; code copied inside the package
itself is not detected. Member names with ``.`` or ``..`` segments, a leading
``/`` or a backslash are refused, so no name can step around these checks. The
other files under ``licenses/`` and the declared ``License-File`` entries are
recorded with the results.

The reference must have LF line endings: a CRLF one is refused. A Windows
checkout with ``core.autocrlf=true`` has a CRLF work-tree LICENSE and packs
CRLF into both distributions, so comparing the two would pass. Build from an LF
checkout or a ``git -c core.autocrlf=false archive`` export, and pass the
export's LICENSE.

The exit code is 0 when every check passes and 1 otherwise. With ``--report``
the results are also written as JSON, whether they pass or fail.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import sys
import tarfile
import zipfile
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXPRESSION = "Apache-2.0"
LICENSE_FILE = "LICENSE"


@dataclass
class Result:
    """The checks of one distribution file."""

    artifact: str
    sha256: str = ""
    failures: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _headers(raw: bytes) -> email.message.Message:
    # METADATA is UTF-8. Parsed as bytes, a non-ASCII value would come back as an
    # email.header.Header object instead of text.
    return email.parser.HeaderParser().parsestr(raw.decode("utf-8", errors="replace"))


def _check_license(data: bytes, member: str, reference: bytes, result: Result) -> None:
    """``member``'s bytes must equal the reference LICENSE."""
    if data != reference:
        result.failures.append(
            f"{member} differs from the reference LICENSE"
            f" (sha256 {_sha256(data)[:12]}, reference {_sha256(reference)[:12]})"
        )


def _check_metadata(raw: bytes, member: str, result: Result) -> None:
    """``member`` must declare the Apache-2.0 expression and the LICENSE file."""
    headers = _headers(raw)
    expressions = headers.get_all("License-Expression") or []
    if expressions != [EXPRESSION]:
        found = expressions or "missing"
        result.failures.append(f"{member}: License-Expression is {found}, want {EXPRESSION!r}")
    files = headers.get_all("License-File") or []
    if LICENSE_FILE not in files:
        result.failures.append(f"{member}: no 'License-File: {LICENSE_FILE}' (found {files})")
    result.facts[f"{member.split('/', 1)[-1]} License-File"] = ", ".join(sorted(files))


def _check_names(names: list[str], result: Result) -> None:
    """Refuse duplicate names and names that do not say plainly where a file goes."""
    # A second member of the same name could hide behind the one checked.
    if duplicates := sorted(name for name, count in Counter(names).items() if count > 1):
        result.failures.append(f"duplicate members: {duplicates}")
    unclear = sorted(
        name
        for name in names
        if name.startswith("/")
        or "\\" in name
        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
    )
    if unclear:
        result.failures.append(f"member names with '.', '..', '/' or '\\' segments: {unclear}")


def _zip_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """The bytes of file member ``name``, or None when there is none."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    return None if info.is_dir() else archive.read(info)


def _zip_required(archive: zipfile.ZipFile, name: str, result: Result) -> bytes | None:
    """The bytes of member ``name``; a missing one is recorded as a failure."""
    data = _zip_member(archive, name)
    if data is None:
        result.failures.append(f"missing member {name}")
    return data


def _tar_required(
    archive: tarfile.TarFile, by_name: dict[str, tarfile.TarInfo], name: str, result: Result
) -> bytes | None:
    """The bytes of regular-file member ``name``; anything else is recorded as a failure."""
    member = by_name.get(name)
    if member is None:
        result.failures.append(f"missing member {name}")
        return None
    handle = archive.extractfile(member) if member.isfile() else None
    if handle is None:
        # A link would have the check read some other member's bytes.
        result.failures.append(f"{name} is not a regular file")
        return None
    return handle.read()


def _wheel_checks(
    archive: zipfile.ZipFile, package: str, reference: bytes, result: Result
) -> None:
    names = archive.namelist()
    _check_names(names, result)
    tops = sorted({name.split("/", 1)[0] for name in names})
    dist_infos = [top for top in tops if top.endswith(".dist-info")]
    if len(dist_infos) != 1:
        result.failures.append(f"want one top-level .dist-info, found {dist_infos}")
        return
    dist_info = dist_infos[0]
    if package not in tops:
        result.failures.append(f"no top-level package {package}/")
    if extra := [top for top in tops if top not in (package, dist_info)]:
        result.failures.append(
            f"top-level entries other than {package}/ and {dist_info}/: {extra}"
        )
    name = f"{dist_info}/licenses/{LICENSE_FILE}"
    if (data := _zip_required(archive, name, result)) is not None:
        _check_license(data, name, reference, result)
    name = f"{dist_info}/METADATA"
    if (data := _zip_required(archive, name, result)) is not None:
        _check_metadata(data, name, result)
    # Files beside LICENSE under licenses/ are not checked; listing them makes one visible.
    licenses = f"{dist_info}/licenses/"
    result.facts["licenses/"] = ", ".join(
        sorted(name.removeprefix(licenses) for name in names if name.startswith(licenses))
    )
    # Which build backend made it: hatchling is not pinned (build-system.requires).
    wheel_file = _zip_member(archive, f"{dist_info}/WHEEL")
    if wheel_file is not None:
        result.facts["generator"] = _headers(wheel_file).get("Generator") or "not stated"


def _sdist_checks(archive: tarfile.TarFile, reference: bytes, result: Result) -> None:
    members = archive.getmembers()
    _check_names([member.name for member in members], result)
    roots = sorted({member.name.split("/", 1)[0] for member in members})
    if len(roots) != 1:
        result.failures.append(f"want one top-level directory, found {roots}")
        return
    by_name = {member.name: member for member in members}
    name = f"{roots[0]}/{LICENSE_FILE}"
    if (data := _tar_required(archive, by_name, name, result)) is not None:
        _check_license(data, name, reference, result)
    name = f"{roots[0]}/PKG-INFO"
    if (data := _tar_required(archive, by_name, name, result)) is not None:
        _check_metadata(data, name, result)


def check_wheel(path: Path, reference: bytes, package: str) -> Result:
    """Check one wheel's LICENSE, METADATA and top-level entries."""
    result = Result(path.name)
    try:
        result.sha256 = _sha256(path.read_bytes())
        with zipfile.ZipFile(path) as archive:
            _wheel_checks(archive, package, reference, result)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, OSError) as exc:
        # RuntimeError: an encrypted member; NotImplementedError: an unknown compression.
        result.failures.append(f"not a readable wheel: {type(exc).__name__}: {exc}")
    return result


def check_sdist(path: Path, reference: bytes) -> Result:
    """Check one sdist's root LICENSE and PKG-INFO."""
    result = Result(path.name)
    try:
        result.sha256 = _sha256(path.read_bytes())
        with tarfile.open(path, "r:gz") as archive:
            _sdist_checks(archive, reference, result)
    except (tarfile.TarError, EOFError, zlib.error, OSError) as exc:
        # EOFError: a truncated archive, which tarfile does not turn into a TarError.
        result.failures.append(f"not a readable sdist: {type(exc).__name__}: {exc}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--license", type=Path, default=Path(LICENSE_FILE))
    parser.add_argument("--package", default="machina")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    failures: list[str] = []
    results: list[Result] = []
    reference = b""
    try:
        reference = args.license.read_bytes()
    except OSError as exc:
        failures.append(f"cannot read the reference LICENSE {args.license}: {exc}")
    else:
        if not reference:
            failures.append(f"the reference LICENSE {args.license} is empty")
        elif b"\r" in reference:
            failures.append(
                f"the reference LICENSE {args.license} has CR line endings, as a"
                " core.autocrlf=true checkout writes it: pass an LF copy (see --help)"
            )
    wheels, sdists = sorted(args.dist.glob("*.whl")), sorted(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        failures.append(
            f"want one wheel and one sdist in {args.dist}, found {len(wheels)} and {len(sdists)}"
        )
    elif not failures:
        results = [
            check_wheel(wheels[0], reference, args.package),
            check_sdist(sdists[0], reference),
        ]

    for result in results:
        failures += [f"{result.artifact}: {failure}" for failure in result.failures]
        status = "FAIL" if result.failures else "ok  "
        facts = "".join(f", {key}: {value}" for key, value in sorted(result.facts.items()))
        print(f"{status} {result.artifact} (sha256 {result.sha256[:12]}{facts})")
    for failure in failures:
        print(f"FAIL {failure}")
    if not failures:
        print(f"LICENSE ({_sha256(reference)[:12]}) and licence metadata check out in both")

    if args.report:
        report = {
            "status": "fail" if failures else "pass",
            "reference_license_sha256": _sha256(reference) if reference else None,
            "artifacts": [asdict(result) for result in results],
            "failures": failures,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes((json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
