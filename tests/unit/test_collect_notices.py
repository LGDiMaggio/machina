"""Tests for ``scripts/compliance/collect_notices.py``.

Each test lays out a fake ``site-packages`` in ``tmp_path``: ``*.dist-info`` directories with
METADATA and RECORD, shaped like what pip installs, and the files they list. The layouts mirror
real cases from the licence audit (botocore's NOTICE at the top of its dist-info, opencv's
LICENSE-3RD-PARTY.txt, onnxruntime's ThirdPartyNotices.txt in its package directory). Tests
that need a symbolic link skip where the OS does not allow creating one.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from scripts.compliance import collect_notices

if TYPE_CHECKING:
    from pathlib import Path

LGPL = b"GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3\n"
# The content of files outside site-packages: it must never reach the bundle.
SECRET = b"sentinel 7f3a: a file outside site-packages\n"


def _site(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "site-packages"
    site.mkdir(parents=True)
    return site


def _metadata(name: str, version: str, *headers: str) -> bytes:
    return (
        "\n".join(
            ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}", *headers]
        ).encode()
        + b"\n\n"
    )


def _dist(
    site: Path,
    name: str,
    version: str,
    files: dict[str, bytes] | None = None,
    *,
    headers: tuple[str, ...] = (),
    metadata: bool = True,
    record: bool = True,
    record_extra: tuple[str, ...] = (),
) -> Path:
    """One installed distribution; ``files`` maps paths relative to site-packages to bytes."""
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    listed = [f"{dist_info.name}/RECORD"]
    if metadata:
        (dist_info / "METADATA").write_bytes(_metadata(name, version, *headers))
        listed.append(f"{dist_info.name}/METADATA")
    for relative, data in (files or {}).items():
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        listed.append(relative)
    if record:
        rows = [f"{entry},sha256=x,1" for entry in [*listed, *record_extra]]
        (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return dist_info


def _run(site: Path, out: Path) -> int:
    return collect_notices.main(["--site-packages", str(site), "--out", str(out)])


def _index(out: Path) -> dict[str, dict[str, object]]:
    document = json.loads((out / "INDEX.json").read_text(encoding="utf-8"))
    return {item["name"]: item for item in document["distributions"]}


def _tree(out: Path) -> dict[str, bytes]:
    return {
        path.relative_to(out).as_posix(): path.read_bytes()
        for path in sorted(out.rglob("*"))
        if path.is_file()
    }


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symbolic link here: {exc}")


# -------------------------------------------------------------- C1, C2: what gets collected


def test_files_under_licenses_are_copied_with_one_index_row(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(
        site,
        "asyncua",
        "2.1.0",
        {
            "asyncua-2.1.0.dist-info/licenses/COPYING": LGPL,
            "asyncua-2.1.0.dist-info/licenses/NOTICE": b"notice\n",
        },
    )
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["asyncua"]
    assert row["files"] == [
        "asyncua-2.1.0.dist-info/licenses/COPYING",
        "asyncua-2.1.0.dist-info/licenses/NOTICE",
    ]
    assert row["status"] == "licence file found"
    assert (
        out / "asyncua-2.1.0" / "asyncua-2.1.0.dist-info" / "licenses" / "COPYING"
    ).read_bytes() == LGPL


def test_a_notice_at_the_top_of_dist_info_is_collected_through_record(tmp_path: Path) -> None:
    # botocore ships LICENSE.txt and NOTICE next to METADATA, not under licenses/.
    site = _site(tmp_path)
    _dist(
        site,
        "botocore",
        "1.43.118",
        {
            "botocore-1.43.118.dist-info/LICENSE.txt": b"Apache\n",
            "botocore-1.43.118.dist-info/NOTICE": b"Botocore\n",
        },
    )
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["botocore"]["files"] == [
        "botocore-1.43.118.dist-info/LICENSE.txt",
        "botocore-1.43.118.dist-info/NOTICE",
    ]


# --------------------------------------------------------------- C3, C4: the name pattern

NOTICE_NAMES = [
    "cv2/LICENSE-3RD-PARTY.txt",
    "onnxruntime/ThirdPartyNotices.txt",
    "pkg/ThirdParty.txt",
    "pkg/third_party_licenses.txt",
    "pkg/third-party-notices.md",
    "nvidia/cudnn/License.txt",
    "pkg/EULA",
    "pkg/COPYRIGHT",
    "pkg/PATENTS",
    "pkg/AUTHORS.rst",
    "pkg/COPYING.LESSER",
    "pkg/LEGAL.txt",
    "pkg/Licence.md",
    "pkg/notice.html",
]


@pytest.mark.parametrize("relative", NOTICE_NAMES)
def test_a_notice_named_file_listed_in_record_is_collected(tmp_path: Path, relative: str) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", {relative: b"text\n"})
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["pkg"]["files"] == [relative]
    assert (out / "pkg-1.0" / relative).read_bytes() == b"text\n"


CODE_NAMES = [
    "pkg/license.py",
    "pkg/__pycache__/license.cpython-311.pyc",
    "pkg/_copyright.cpython-311-x86_64-linux-gnu.so",
    "pkg/libnotice.so.1",
    "pkg/notice.pyi",
    "pkg/static/license-badge.png",
    "pkg/fonts/Copyright.ttf",
]


@pytest.mark.parametrize("relative", CODE_NAMES)
def test_code_and_media_files_are_not_collected_even_when_named_like_notices(
    tmp_path: Path, relative: str
) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", {relative: b"\x00code"})
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["pkg"]["files"] == []


def test_files_with_ordinary_names_are_not_collected(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(
        site,
        "pkg",
        "1.0",
        {
            "pkg/README.md": b"r",
            "pkg/data.json": b"{}",
            "pkg-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        },
    )
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["pkg"]["files"] == []


# ---------------------------------------------------------------------- C5: nothing to collect


def test_a_distribution_without_licence_files_is_listed_not_skipped(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(
        site,
        "tokenizers",
        "0.22.0",
        {"tokenizers/__init__.py": b""},
        headers=("License-Expression: Apache-2.0",),
    )
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["tokenizers"]
    assert row["status"] == "no licence file found"
    assert row["files"] == []
    assert row["license_expression"] == "Apache-2.0"
    document = json.loads((out / "INDEX.json").read_text(encoding="utf-8"))
    assert (document["no_licence_file_found"], document["total"]) == (1, 1)
    assert not (out / "tokenizers-0.22.0").exists()


# ------------------------------------------------------------------- C6: unsafe paths and links


def test_a_record_entry_outside_site_packages_is_not_followed(tmp_path: Path) -> None:
    site = _site(tmp_path)
    outside = tmp_path / "venv" / "bin" / "LICENSE"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(SECRET)
    _dist(site, "pkg", "1.0", record_extra=("../../bin/LICENSE", "../../../secret/NOTICE"))
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["pkg"]
    assert row["files"] == []
    assert any("not read, outside site-packages" in problem for problem in row["problems"])
    assert SECRET not in b"".join(_tree(out).values())


@pytest.mark.parametrize(
    "make_entry",
    [
        lambda outside: str(outside),
        lambda outside: "/etc/LICENSE",
        lambda outside: "C:\\Windows\\LICENSE.txt",
        lambda outside: "C:LICENSE",
    ],
)
def test_an_absolute_record_entry_is_not_followed(tmp_path: Path, make_entry: object) -> None:
    site = _site(tmp_path)
    outside = tmp_path / "LICENSE"
    outside.write_bytes(SECRET)
    entry = make_entry(outside)  # type: ignore[operator]
    _dist(site, "pkg", "1.0", record_extra=(entry,))
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["pkg"]["files"] == []
    assert SECRET not in b"".join(_tree(out).values())


def test_a_link_inside_site_packages_to_a_file_outside_is_not_followed(tmp_path: Path) -> None:
    site = _site(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(SECRET)
    dist_info = _dist(site, "pkg", "1.0", record_extra=("pkg/LICENSE",))
    (site / "pkg").mkdir()
    _symlink(site / "pkg" / "LICENSE", outside)
    (dist_info / "licenses").mkdir()
    _symlink(dist_info / "licenses" / "NOTICE", outside)
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["pkg"]
    assert row["files"] == []
    assert any("not read, outside site-packages" in problem for problem in row["problems"])
    assert SECRET not in b"".join(_tree(out).values())


def test_a_linked_directory_under_licenses_is_reported_not_followed(tmp_path: Path) -> None:
    site = _site(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "LICENSE").write_bytes(SECRET)
    dist_info = _dist(site, "pkg", "1.0", {"pkg-1.0.dist-info/licenses/LICENSE": b"own\n"})
    _symlink(dist_info / "licenses" / "vendored", outside, directory=True)
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["pkg"]
    assert row["files"] == ["pkg-1.0.dist-info/licenses/LICENSE"]
    assert any("linked directories not followed" in problem for problem in row["problems"])
    assert SECRET not in b"".join(_tree(out).values())


# ------------------------------------------------------------- C7, C8: deterministic, verbatim


def test_two_runs_over_the_same_input_give_identical_bytes(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "Zeta", "2.0", {"zeta/NOTICE": b"z\n"}, headers=("License-Expression: MIT",))
    _dist(site, "alpha", "1.0", {"alpha-1.0.dist-info/licenses/LICENSE": b"a\n"})
    _dist(site, "middle", "3.0", {"middle/__init__.py": b""})
    first, second = tmp_path / "first", tmp_path / "second"
    assert _run(site, first) == 0
    assert _run(site, second) == 0
    assert _tree(first) == _tree(second)
    names = [item["name"] for item in json.loads(_tree(first)["INDEX.json"])["distributions"]]
    assert names == ["alpha", "middle", "Zeta"]
    for text in (_tree(first)["INDEX.json"], _tree(first)["README.txt"]):
        # Any spelling of the absolute path: native, POSIX, and JSON-escaped (Windows).
        for spelling in (str(tmp_path), tmp_path.as_posix(), json.dumps(str(tmp_path))[1:-1]):
            assert spelling.encode() not in text
        assert b"\r\n" not in text


def test_the_index_is_sorted_by_declared_name_not_by_directory(tmp_path: Path) -> None:
    # Directory order differs from name order on every platform: aaa-… declares "zzz".
    site = _site(tmp_path)
    dist_info = _dist(site, "aaa", "1.0")
    (dist_info / "METADATA").write_bytes(_metadata("zzz", "1.0"))
    _dist(site, "mmm", "1.0")
    out = tmp_path / "out"
    assert _run(site, out) == 0
    document = json.loads((out / "INDEX.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in document["distributions"]] == ["mmm", "zzz"]


def test_a_non_utf8_licence_file_is_copied_byte_for_byte(tmp_path: Path) -> None:
    site = _site(tmp_path)
    data = b"Copyright \xe9\xff\x00 Latin-1 and binary bytes\r\n"
    _dist(site, "pkg", "1.0", {"pkg-1.0.dist-info/licenses/LICENSE": data})
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert (out / "pkg-1.0" / "pkg-1.0.dist-info" / "licenses" / "LICENSE").read_bytes() == data


# ------------------------------------------------------------ C9: broken distribution metadata


def test_a_dist_info_without_metadata_is_listed_with_the_problem(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", {"pkg-1.0.dist-info/licenses/LICENSE": b"l\n"}, metadata=False)
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["pkg"]
    assert (row["name"], row["version"]) == ("pkg", "1.0")
    assert row["files"] == ["pkg-1.0.dist-info/licenses/LICENSE"]
    assert any("METADATA missing" in problem for problem in row["problems"])


def test_a_dist_info_without_record_still_yields_its_licenses_folder(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(
        site,
        "pkg",
        "1.0",
        {"pkg-1.0.dist-info/licenses/LICENSE": b"l\n", "pkg/NOTICE": b"n\n"},
        record=False,
    )
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["pkg"]
    assert row["files"] == ["pkg-1.0.dist-info/licenses/LICENSE"]
    assert "RECORD missing or outside site-packages" in row["problems"]


def test_an_unreadable_record_is_reported(tmp_path: Path) -> None:
    site = _site(tmp_path)
    dist_info = _dist(site, "pkg", "1.0")
    (dist_info / "RECORD").write_bytes(b"pkg/NOTICE,sha256=x,1\n\xff\xfe not utf-8\n")
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert "RECORD unreadable (UnicodeDecodeError)" in _index(out)["pkg"]["problems"]


def test_a_record_entry_for_a_missing_notice_is_reported(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", record_extra=("pkg/LICENSE",))
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert "listed in RECORD but missing: 'pkg/LICENSE'" in _index(out)["pkg"]["problems"]


# -------------------------------------------------------- C10: declared fields, never inferred


def test_declared_licence_fields_are_copied_and_nothing_is_inferred(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(
        site,
        "declared",
        "1.0",
        headers=(
            "License-Expression: MIT AND (Apache-2.0 OR BSD-2-Clause)",
            "Classifier: License :: OSI Approved :: MIT License",
            "Classifier: Programming Language :: Python :: 3",
        ),
    )
    _dist(
        site,
        "fulltext",
        "1.0",
        headers=("License: Copyright (c) Someone\n        Permission is hereby granted",),
    )
    # The text says MIT, the metadata says nothing: the index must not conclude MIT.
    _dist(site, "silent", "1.0", {"silent-1.0.dist-info/licenses/LICENSE": b"MIT License\n"})
    out = tmp_path / "out"
    assert _run(site, out) == 0
    index = _index(out)
    assert index["declared"]["license_expression"] == "MIT AND (Apache-2.0 OR BSD-2-Clause)"
    assert index["declared"]["license_classifiers"] == ["License :: OSI Approved :: MIT License"]
    assert index["fulltext"]["license"] == "Copyright (c) Someone"
    silent = index["silent"]
    assert (silent["license_expression"], silent["license"], silent["license_classifiers"]) == (
        None,
        None,
        [],
    )


# ---------------------------------------------------------------- C11 and the command line


def test_a_non_empty_output_directory_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", {"pkg-1.0.dist-info/licenses/LICENSE": b"l\n"})
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_bytes(b"keep")
    assert _run(site, out) == 1
    assert "refusing to write into" in capsys.readouterr().out
    assert _tree(out) == {"keep.txt": b"keep"}


def test_an_empty_output_directory_is_used(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0")
    out = tmp_path / "out"
    out.mkdir()
    assert _run(site, out) == 0
    assert (out / "INDEX.json").is_file()


def test_a_missing_site_packages_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path / "nowhere", tmp_path / "out") == 1
    assert "no such directory" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_a_site_packages_without_distributions_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = _site(tmp_path)
    assert _run(site, tmp_path / "out") == 1
    assert "no *.dist-info directory" in capsys.readouterr().out


def test_the_readme_says_what_the_bundle_is_not(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "with", "1.0", {"with-1.0.dist-info/licenses/LICENSE": b"l\n"})
    _dist(site, "without", "1.0")
    out = tmp_path / "out"
    assert _run(site, out) == 0
    readme = (out / "README.txt").read_text(encoding="utf-8")
    assert "not a complete\nnotice pack and not legal advice" in readme
    assert "For 1 of 2 distributions no licence file was found" in readme
    assert "such as source code or an offer\n  of it; this bundle covers none of that" in readme


# ------------------------------------------------------- review findings: what once broke the run


def test_non_ascii_metadata_values_are_copied_as_text(tmp_path: Path) -> None:
    # Parsed as bytes, a non-ASCII header value is an email.header.Header, not text.
    site = _site(tmp_path)
    _dist(
        site,
        "scipy",
        "1.17.1",
        headers=("License: Copyright (c) Wolfgang Hörmann", "License-File: LICENSE.txt"),
    )
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["scipy"]
    assert row["license"] == "Copyright (c) Wolfgang Hörmann"
    assert row["license_files_declared"] == ["LICENSE.txt"]


def test_a_link_loop_is_reported_not_raised(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", record_extra=("pkg/LICENSE",))
    (site / "pkg").mkdir()
    _symlink(site / "pkg" / "LICENSE", site / "pkg" / "LICENSE")
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["pkg"]["files"] == []


def test_a_nul_in_a_record_entry_is_reported_not_raised(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", record_extra=("pkg/LICENSE\x00x",))
    out = tmp_path / "out"
    assert _run(site, out) == 0
    row = _index(out)["pkg"]
    assert any("unresolvable" in problem for problem in row["problems"])


def test_a_dist_info_named_dot_dot_is_not_read_and_nothing_lands_outside_out(
    tmp_path: Path,
) -> None:
    # "...dist-info" has the stem "..": its folder would be out/.., outside --out.
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0")
    planted = site / "...dist-info"
    (planted / "licenses").mkdir(parents=True)
    (planted / "licenses" / "LICENSE").write_bytes(SECRET)
    out = tmp_path / "evidence" / "out"
    assert _run(site, out) == 0
    document = json.loads((out / "INDEX.json").read_text(encoding="utf-8"))
    assert document["not_indexed"] == [
        {"path": "...dist-info", "reason": "not a valid dist-info name"}
    ]
    outside = [
        p for p in (tmp_path / "evidence").rglob("*") if p.is_file() and out not in p.parents
    ]
    assert outside == []


@pytest.mark.parametrize("linked", ["RECORD", "METADATA", "licenses", "dist-info"])
def test_linked_metadata_is_not_read_through(tmp_path: Path, linked: str) -> None:
    site = _site(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # Each outside file carries a marker where it would surface if read: the RECORD's
    # entry name, the METADATA's name, the LICENSE's text.
    (elsewhere / "RECORD").write_bytes(b"pkg/NOTICE-leaked-record,,\n")
    (elsewhere / "METADATA").write_bytes(
        _metadata("leaked", "9.9", "License: " + SECRET.decode().strip())
    )
    (elsewhere / "LICENSE").write_bytes(SECRET)
    (elsewhere / "licenses").mkdir()
    (elsewhere / "licenses" / "LICENSE-leaked-listing").write_bytes(SECRET)
    if linked == "dist-info":
        _symlink(site / "pkg-1.0.dist-info", elsewhere, directory=True)
        _dist(site, "other", "1.0")
    else:
        dist_info = _dist(
            site, "pkg", "1.0", record=linked != "RECORD", metadata=linked != "METADATA"
        )
        if linked == "licenses":
            _symlink(dist_info / "licenses", elsewhere, directory=True)
        else:
            _symlink(dist_info / linked, elsewhere / linked)
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert SECRET.strip() not in b"".join(_tree(out).values())
    # Nothing read through the link, not even a file name in a problem message.
    assert b"leaked" not in _tree(out)["INDEX.json"]
    document = json.loads(_tree(out)["INDEX.json"])
    if linked == "dist-info":
        assert document["not_indexed"] == [
            {"path": "pkg-1.0.dist-info", "reason": "a link, not followed"}
        ]
    elif linked == "licenses":
        assert (
            "linked directories not followed: ['pkg-1.0.dist-info/licenses']"
            in _index(out)["pkg"]["problems"]
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold a newline")
def test_an_invalid_dist_info_name_is_printed_quoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0")
    (site / "x\n::error::forged.dist-info").mkdir()
    out = tmp_path / "out"
    assert _run(site, out) == 0
    printed = capsys.readouterr().out
    assert not [line for line in printed.splitlines() if line.startswith("::")]
    assert json.loads((out / "INDEX.json").read_text(encoding="utf-8"))["not_indexed"][0][
        "reason"
    ] == ("not a valid dist-info name")


def test_record_entries_are_printed_quoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A newline in an entry must not start a line the CI runner reads as a command.
    site = _site(tmp_path)
    dist_info = _dist(site, "pkg", "1.0")
    (dist_info / "RECORD").write_text('"x\n::error::LICENSE",,\n', encoding="utf-8")
    out = tmp_path / "out"
    assert _run(site, out) == 0
    printed = capsys.readouterr().out
    assert not [line for line in printed.splitlines() if line.startswith("::")]


def test_only_licence_terms_count_as_a_licence_file(tmp_path: Path) -> None:
    # AUTHORS and NOTICE are copied, but a distribution with only those has no licence file.
    site = _site(tmp_path)
    _dist(site, "credits", "1.0", {"credits/AUTHORS": b"a\n", "credits/NOTICE": b"n\n"})
    _dist(site, "terms", "1.0", {"terms/COPYING.LESSER": b"l\n"})
    out = tmp_path / "out"
    assert _run(site, out) == 0
    index = _index(out)
    assert index["credits"]["files"] == ["credits/AUTHORS", "credits/NOTICE"]
    assert index["credits"]["status"] == "no licence file found"
    assert index["terms"]["status"] == "licence file found"


def test_legacy_egg_info_installs_are_listed_as_not_read(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0")
    (site / "legacy-2.0.egg-info").mkdir()
    out = tmp_path / "out"
    assert _run(site, out) == 0
    document = json.loads((out / "INDEX.json").read_text(encoding="utf-8"))
    assert document["not_indexed"] == [
        {"path": "legacy-2.0.egg-info", "reason": "legacy .egg-info install, not read"}
    ]
    assert document["total"] == 1


def test_a_backslash_record_entry_is_read_as_a_relative_path(tmp_path: Path) -> None:
    # A RECORD written on Windows may use backslashes.
    site = _site(tmp_path)
    _dist(site, "pkg", "1.0", {"pkg/NOTICE": b"n\n"}, record=False)
    (site / "pkg-1.0.dist-info" / "RECORD").write_text("pkg\\NOTICE,,\n", encoding="utf-8")
    out = tmp_path / "out"
    assert _run(site, out) == 0
    assert _index(out)["pkg"]["files"] == ["pkg/NOTICE"]
