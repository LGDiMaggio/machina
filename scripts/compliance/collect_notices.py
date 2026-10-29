"""Collect the licence and notice files of every distribution in a site-packages.

    python scripts/compliance/collect_notices.py --site-packages <dir> --out <dir>

For each ``*.dist-info`` directory in ``--site-packages`` it copies:

* every file under ``<name>.dist-info/licenses/`` (PEP 639), and
* every file its RECORD lists whose name contains licence or license, copying,
  notice, third-party, copyright, legal, patent(s), EULA or authors, in any
  case. That reaches the notices of bundled components kept outside
  ``licenses/``, such as opencv's ``LICENSE-3RD-PARTY.txt`` or onnxruntime's
  ``ThirdPartyNotices.txt``. Code, compiled and image files are skipped even
  when their name matches.

Each file is copied byte for byte to ``<out>/<dist-info name without
.dist-info>/<path in site-packages>``. ``INDEX.json`` lists every distribution,
sorted by name: its name and version, the licence fields its METADATA declares
(``License-Expression``, ``License-File`` and the licence classifiers as
stated; of ``License``, which may hold a whole text, its first line, cut at 200
characters), the files collected, and any problem met. A distribution with no
file under ``licenses/`` and none with a licence-like name (licence or license,
copying, EULA) is marked "no licence file found", never left out. Legacy
``*.egg-info`` installs are listed as not read.

Nothing is inferred: no licence is concluded from a file's text and no missing
text is supplied. Only files that resolve inside ``--site-packages`` are read,
so a RECORD entry or a link pointing elsewhere is skipped and reported, and
nothing is written outside ``--out``. The output holds no timestamps or
absolute paths, so the same environment gives the same bytes. ``--out`` must
be missing or empty.

The bundle is evidence of the licence and notice files found in the installed
distributions. It is not a complete notice pack and not legal advice: the
README.txt written with it says so.
"""

from __future__ import annotations

import argparse
import csv
import email.parser
import io
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

NOTICE_NAME = re.compile(
    r"licen[cs]e|copying|notice|third[_-]?party|copyright|legal|patents?|eula|authors", re.I
)
# Of the notice names, those that hold licence terms rather than credits or notices.
LICENCE_NAME = re.compile(r"licen[cs]e|copying|eula", re.I)
# Matching names that are not notices: modules, compiled code, images and fonts.
SKIPPED_SUFFIXES = frozenset(
    {".py", ".pyc", ".pyo", ".pyi", ".pyd", ".pyx", ".pxd", ".so", ".dll", ".dylib", ".exe"}
    | {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".ttf", ".otf", ".woff", ".woff2"}
)
# A dist-info directory's name without the suffix: "<name>-<version>", never "." or "..".
DIST_INFO_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
NO_FILE = "no licence file found"
FOUND = "licence file found"
README = """\
Licence and notice files found in the distributions installed in one Python
environment, collected by scripts/compliance/collect_notices.py.

Each folder holds the files of one distribution, byte for byte, under their
path in site-packages. INDEX.json lists every distribution with the licence
fields its metadata declares and the files collected.

This bundle is evidence of the licence and notice files found, not a complete
notice pack and not legal advice:
- For {missing} of {total} distributions no licence file was found: none under
  licenses/ and none with a licence-like name in RECORD. They are marked
  "no licence file found" in INDEX.json; their licence texts have to be
  obtained from upstream, and nothing here supplies or infers them.
- Declared licence fields are copied as the metadata states them (the License
  field only by its first line). They are not checked against the files.
- Some licences require more than their text, such as source code or an offer
  of it; this bundle covers none of that.
- Only Python distributions with a .dist-info directory are covered. Legacy
  .egg-info installs, operating-system packages, the Python interpreter, and
  anything else in an image are not.
"""


def _is_notice(path: PurePosixPath) -> bool:
    if path.suffix.lower() in SKIPPED_SUFFIXES or ".so." in path.name:
        return False
    return bool(NOTICE_NAME.search(path.name))


def _inside(site: Path, entry: str) -> Path | None:
    """The path ``entry`` names, resolved, if it lies inside ``site``; else None.

    Resolving follows ``..`` and links, and an absolute path (or, on Windows, a
    drive path) replaces ``site`` when joined, so each of them that leads out of
    ``site`` is refused. So is one that cannot be resolved (a link loop, a NUL).
    """
    try:
        candidate = site.joinpath(*PurePosixPath(entry.replace("\\", "/")).parts).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_relative_to(site) else None


def _read_inside(site: Path, path: Path) -> bytes | None:
    """The bytes of ``path`` if it is a file that resolves inside ``site``; else None."""
    target = _inside(site, path.relative_to(site).as_posix())
    return target.read_bytes() if target is not None and target.is_file() else None


def _record_entries(site: Path, dist_info: Path, problems: list[str]) -> list[str]:
    raw = _read_inside(site, dist_info / "RECORD")
    if raw is None:
        problems.append("RECORD missing or outside site-packages")
        return []
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        problems.append(f"RECORD unreadable ({type(exc).__name__})")
        return []
    return [row[0] for row in rows if row and row[0]]


def _metadata(site: Path, dist_info: Path, problems: list[str]) -> dict[str, object]:
    stem_name, _, stem_version = dist_info.name.removesuffix(".dist-info").rpartition("-")
    fields: dict[str, object] = {
        "name": stem_name or stem_version,
        "version": stem_version if stem_name else None,
        "license_expression": None,
        "license": None,
        "license_files_declared": [],
        "license_classifiers": [],
    }
    raw = _read_inside(site, dist_info / "METADATA")
    if raw is None:
        problems.append("METADATA missing; name and version taken from the dist-info name")
        return fields
    # METADATA is UTF-8. Parsed as bytes, a non-ASCII value would come back as an
    # email.header.Header object instead of text.
    headers = email.parser.HeaderParser().parsestr(raw.decode("utf-8", errors="replace"))
    fields["name"] = headers.get("Name") or fields["name"]
    fields["version"] = headers.get("Version") or fields["version"]
    fields["license_expression"] = headers.get("License-Expression")
    # The License field may hold a whole licence text: its first line identifies it.
    license_field = (headers.get("License") or "").strip()
    fields["license"] = license_field.splitlines()[0][:200] if license_field else None
    fields["license_files_declared"] = sorted(headers.get_all("License-File") or [])
    fields["license_classifiers"] = sorted(
        value for value in headers.get_all("Classifier") or [] if value.startswith("License ::")
    )
    return fields


def _collect_one(site: Path, dist_info: Path, out: Path) -> dict[str, object]:
    """Copy one distribution's licence and notice files; return its index entry."""
    problems: list[str] = []
    entry = _metadata(site, dist_info, problems)
    wanted: dict[str, Path] = {}
    skipped: set[str] = set()
    unfollowed: list[str] = []
    licenses = dist_info / "licenses"
    if licenses.is_symlink():
        unfollowed.append(licenses.relative_to(site).as_posix())
    elif licenses.is_dir():
        for root, dirs, files in os.walk(licenses):
            dirs.sort()
            # os.walk does not descend into linked directories: say so rather than drop them.
            unfollowed += [
                Path(root, name).relative_to(site).as_posix()
                for name in dirs
                if Path(root, name).is_symlink()
            ]
            for name in files:
                relative = Path(root, name).relative_to(site).as_posix()
                target = _inside(site, relative)
                if target is None:
                    skipped.add(relative)
                elif target.is_file():
                    wanted[target.relative_to(site).as_posix()] = target
    for record_entry in _record_entries(site, dist_info, problems):
        if not _is_notice(PurePosixPath(record_entry.replace("\\", "/"))):
            continue
        target = _inside(site, record_entry)
        if target is None:
            skipped.add(record_entry)
        elif target.is_file():
            wanted[target.relative_to(site).as_posix()] = target
        elif not target.exists():
            problems.append(f"listed in RECORD but missing: {record_entry!r}")
    if skipped:
        problems.append(f"not read, outside site-packages or unresolvable: {sorted(skipped)}")
    if unfollowed:
        problems.append(f"linked directories not followed: {sorted(unfollowed)}")
    folder = (out / dist_info.name.removesuffix(".dist-info")).resolve()
    for relative, source in sorted(wanted.items()):
        destination = (folder / relative).resolve()
        if not destination.is_relative_to(folder):
            problems.append(f"not copied, it would land outside its folder: {relative!r}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    licences_dir = f"{dist_info.name}/licenses/"
    found = any(
        relative.startswith(licences_dir) or LICENCE_NAME.search(PurePosixPath(relative).name)
        for relative in wanted
    )
    entry.update(
        dist_info=dist_info.name,
        files=sorted(wanted),
        status=FOUND if found else NO_FILE,
        problems=problems,
    )
    return entry


def collect(site: Path, out: Path) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Copy each distribution's licence and notice files into ``out``.

    Returns the index entries and the directories that were not read, with why.
    """
    index: list[dict[str, object]] = []
    not_indexed: list[dict[str, str]] = []
    for path in sorted(site.iterdir()):
        if path.name.endswith(".egg-info"):
            not_indexed.append({"path": path.name, "reason": "legacy .egg-info install, not read"})
            continue
        if not path.name.endswith(".dist-info"):
            continue
        if path.is_symlink():
            not_indexed.append({"path": path.name, "reason": "a link, not followed"})
        elif not DIST_INFO_STEM.fullmatch(path.name.removesuffix(".dist-info")):
            not_indexed.append({"path": path.name, "reason": "not a valid dist-info name"})
        elif path.is_dir():
            index.append(_collect_one(site, path, out))
    index.sort(
        key=lambda item: (str(item["name"]).lower(), str(item["version"]), item["dist_info"])
    )
    return index, sorted(not_indexed, key=lambda item: item["path"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    site = args.site_packages.resolve()
    if not site.is_dir():
        print(f"FAIL no such directory: {str(args.site_packages)!r}")
        return 1
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        print(f"FAIL refusing to write into {str(args.out)!r}: it is not an empty directory")
        return 1
    if not any(path.is_dir() for path in site.glob("*.dist-info")):
        print(f"FAIL no *.dist-info directory in {str(args.site_packages)!r}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    index, not_indexed = collect(site, args.out)
    missing = sum(1 for item in index if item["status"] == NO_FILE)
    document = {
        "distributions": index,
        "no_licence_file_found": missing,
        "not_indexed": not_indexed,
        "total": len(index),
    }
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    (args.out / "INDEX.json").write_bytes(payload.encode("utf-8"))
    readme = README.format(missing=missing, total=len(index))
    (args.out / "README.txt").write_bytes(readme.encode("utf-8"))
    print(f"{len(index)} distributions, {missing} with {NO_FILE}; bundle in {str(args.out)!r}")
    # Unvalidated names and RECORD entries are quoted (here and where each problem
    # is worded): a newline in one must not start a line that the CI runner would
    # read as a workflow command. Indexed dist-info names match DIST_INFO_STEM.
    for item in not_indexed:
        print(f"note {item['path']!r}: {item['reason']}")
    for item in index:
        if item["problems"]:
            print(f"note {item['dist_info']}: {'; '.join(map(str, item['problems']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
