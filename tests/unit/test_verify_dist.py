"""Tests for ``scripts/compliance/verify_dist.py``.

Each test builds a wheel (``zipfile``) and an sdist (``tarfile``) in ``tmp_path``, shaped like
hatchling's output, and checks the exit code and the message naming what is wrong. The LICENSE
text here is a stand-in: the check compares bytes with whatever ``--license`` points at.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest
from scripts.compliance import verify_dist

if TYPE_CHECKING:
    from pathlib import Path

LICENSE = b"Stand-in licence text\n\nline two\n"
DIST_INFO = "machina_ai-0.4.0.dist-info"
SDIST_ROOT = "machina_ai-0.4.0"
WHEEL_NAME = "machina_ai-0.4.0-py3-none-any.whl"
SDIST_NAME = "machina_ai-0.4.0.tar.gz"
# Stands for "a valid PKG-INFO"; None means the sdist has none.
PKG_INFO_DEFAULT = b"<default>"


def _metadata(
    expression: str | None = "Apache-2.0", license_file: str | None = "LICENSE"
) -> bytes:
    lines = ["Metadata-Version: 2.4", "Name: machina-ai", "Version: 0.4.0"]
    if expression is not None:
        lines.append(f"License-Expression: {expression}")
    if license_file is not None:
        lines.append(f"License-File: {license_file}")
    return ("\n".join(lines) + "\n\nLong description.\n").encode()


def _wheel(
    path: Path,
    *,
    license_bytes: bytes | None = LICENSE,
    metadata: bytes | None = None,
    extra: dict[str, bytes] | None = None,
) -> None:
    members = {
        "machina/__init__.py": b"",
        f"{DIST_INFO}/METADATA": _metadata() if metadata is None else metadata,
        f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nGenerator: hatchling 1.29.0\n",
        f"{DIST_INFO}/RECORD": b"",
    }
    if license_bytes is not None:
        members[f"{DIST_INFO}/licenses/LICENSE"] = license_bytes
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def _sdist(
    path: Path,
    *,
    license_bytes: bytes | None = LICENSE,
    pkg_info: bytes | None = PKG_INFO_DEFAULT,
    root: str = SDIST_ROOT,
    extra_root: bool = False,
    license_type: bytes = tarfile.REGTYPE,
    extra: tuple[tuple[str, bytes], ...] = (),
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        if pkg_info is not None:
            _add(
                archive,
                f"{root}/PKG-INFO",
                _metadata() if pkg_info is PKG_INFO_DEFAULT else pkg_info,
            )
        _add(archive, f"{root}/pyproject.toml", b"[project]\nname = 'machina-ai'\n")
        if license_type != tarfile.REGTYPE:
            # A symbolic link, hard link or directory where the LICENSE file should be.
            link = tarfile.TarInfo(f"{root}/LICENSE")
            link.type = license_type
            link.linkname = (
                f"{root}/pyproject.toml" if license_type == tarfile.LNKTYPE else "pyproject.toml"
            )
            archive.addfile(link)
        elif license_bytes is not None:
            _add(archive, f"{root}/LICENSE", license_bytes)
        if extra_root:
            _add(archive, "other-1.0/LICENSE", LICENSE)
        for name, data in extra:
            _add(archive, name, data)


def _layout(tmp_path: Path, *, wheels: int = 1, sdists: int = 1) -> Path:
    """A ``dist/`` with valid distributions, plus the reference LICENSE next to it."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for index in range(wheels):
        _wheel(dist / (WHEEL_NAME if index == 0 else f"other_{index}-1.0-py3-none-any.whl"))
    for index in range(sdists):
        _sdist(dist / (SDIST_NAME if index == 0 else f"other_{index}-1.0.tar.gz"))
    (tmp_path / "LICENSE").write_bytes(LICENSE)
    return dist


def _run(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    args = ["--dist", str(tmp_path / "dist"), "--license", str(tmp_path / "LICENSE"), *extra]
    code = verify_dist.main(args)
    return code, capsys.readouterr().out


# ------------------------------------------------------------------------------------- V1: pass


def test_matching_distributions_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _layout(tmp_path)
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "FAIL" not in out
    assert "generator: hatchling 1.29.0" in out


# -------------------------------------------------------------------- V2-V5: the LICENSE bytes


def test_wheel_without_license_fails_naming_the_member(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, license_bytes=None)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"missing member {DIST_INFO}/licenses/LICENSE" in out


def test_sdist_without_license_fails_naming_the_member(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, license_bytes=None)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"missing member {SDIST_ROOT}/LICENSE" in out


CHANGED = {
    "one byte": LICENSE.replace(b"two", b"twO"),
    "CRLF": LICENSE.replace(b"\n", b"\r\n"),
    "truncated": LICENSE[:-1],
}


@pytest.mark.parametrize("variant", sorted(CHANGED))
def test_wheel_license_that_differs_fails_naming_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], variant: str
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, license_bytes=CHANGED[variant])
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{WHEEL_NAME}: {DIST_INFO}/licenses/LICENSE differs from the reference LICENSE" in out


@pytest.mark.parametrize("variant", sorted(CHANGED))
def test_sdist_license_that_differs_fails_naming_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], variant: str
) -> None:
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, license_bytes=CHANGED[variant])
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{SDIST_NAME}: {SDIST_ROOT}/LICENSE differs from the reference LICENSE" in out


@pytest.mark.parametrize(
    "license_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE],
    ids=["symlink", "hardlink", "directory"],
)
def test_sdist_license_that_is_not_a_regular_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], license_type: bytes
) -> None:
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, license_type=license_type)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{SDIST_ROOT}/LICENSE is not a regular file" in out


# ------------------------------------------------------------- V6, V7, V11: licence metadata

BAD_METADATA = {
    "no expression": (_metadata(expression=None), "License-Expression is missing"),
    "MIT": (_metadata(expression="MIT"), "License-Expression is ['MIT']"),
    "two expressions": (
        _metadata().replace(b"License-File", b"License-Expression: MIT\nLicense-File"),
        "License-Expression is ['Apache-2.0', 'MIT']",
    ),
    "no License-File": (_metadata(license_file=None), "no 'License-File: LICENSE'"),
    "other License-File": (_metadata(license_file="COPYING"), "no 'License-File: LICENSE'"),
}


@pytest.mark.parametrize("variant", sorted(BAD_METADATA))
def test_wheel_metadata_without_the_apache_fields_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], variant: str
) -> None:
    metadata, message = BAD_METADATA[variant]
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, metadata=metadata)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{WHEEL_NAME}: {DIST_INFO}/METADATA: {message}" in out


@pytest.mark.parametrize("variant", sorted(BAD_METADATA))
def test_sdist_pkg_info_without_the_apache_fields_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], variant: str
) -> None:
    pkg_info, message = BAD_METADATA[variant]
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, pkg_info=pkg_info)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{SDIST_NAME}: {SDIST_ROOT}/PKG-INFO: {message}" in out


def test_wheel_without_metadata_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = _layout(tmp_path)
    with zipfile.ZipFile(dist / WHEEL_NAME, "w") as archive:
        archive.writestr("machina/__init__.py", b"")
        archive.writestr(f"{DIST_INFO}/licenses/LICENSE", LICENSE)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"missing member {DIST_INFO}/METADATA" in out


# ------------------------------------------------------------------ V8: wheel top-level entries


@pytest.mark.parametrize("member", ["vendored/__init__.py", "six.py", "machina_extra/x.py"])
def test_wheel_with_another_top_level_entry_fails_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], member: str
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, extra={member: b""})
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "top-level entries other than machina/" in out
    assert repr(member.split("/")[0]) in out


def test_wheel_without_the_package_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME)
    code, out = _run(tmp_path, capsys, "--package", "not_machina")
    assert code == 1
    assert "no top-level package not_machina/" in out


def test_wheel_with_two_dist_infos_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, extra={"six-1.0.dist-info/METADATA": b"Name: six\n"})
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "want one top-level .dist-info" in out


# --------------------------------------------------------------- V9: how many distributions


@pytest.mark.parametrize(("wheels", "sdists"), [(0, 1), (2, 1), (1, 0), (1, 2), (0, 0)])
def test_anything_but_one_wheel_and_one_sdist_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], wheels: int, sdists: int
) -> None:
    _layout(tmp_path, wheels=wheels, sdists=sdists)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert (
        f"want one wheel and one sdist in {tmp_path / 'dist'}, found {wheels} and {sdists}" in out
    )


def test_missing_dist_directory_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "LICENSE").write_bytes(LICENSE)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "found 0 and 0" in out


# ------------------------------------------------------------ V10: the reference LICENSE itself


def test_missing_reference_license_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _layout(tmp_path)
    (tmp_path / "LICENSE").unlink()
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "cannot read the reference LICENSE" in out


def test_empty_reference_license_fails_even_against_empty_members(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, license_bytes=b"")
    _sdist(dist / SDIST_NAME, license_bytes=b"")
    (tmp_path / "LICENSE").write_bytes(b"")
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "is empty" in out


# -------------------------------------------------------------------- malformed distributions


def test_wheel_that_is_not_a_zip_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = _layout(tmp_path)
    (dist / WHEEL_NAME).write_bytes(b"not a zip")
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{WHEEL_NAME}: not a readable wheel" in out


def test_sdist_that_is_not_a_tarball_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    (dist / SDIST_NAME).write_bytes(b"not a tarball")
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{SDIST_NAME}: not a readable sdist" in out


def test_wheel_with_a_duplicate_license_member_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # zipfile reads the last of two same-named members; the first may be what pip installs.
    dist = _layout(tmp_path)
    with (
        zipfile.ZipFile(dist / WHEEL_NAME, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr(f"{DIST_INFO}/licenses/LICENSE", b"other text\n")
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"duplicate members: ['{DIST_INFO}/licenses/LICENSE']" in out


def test_sdist_with_two_top_level_directories_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, extra_root=True)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "want one top-level directory" in out


# ---------------------------------------------------------------------------------- the report


def test_report_records_a_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, license_bytes=None)
    report = tmp_path / "evidence" / "verify-dist.json"
    code, _ = _run(tmp_path, capsys, "--report", str(report))
    assert code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["status"] == "fail"
    assert any("missing member" in failure for failure in data["failures"])
    assert [artifact["artifact"] for artifact in data["artifacts"]] == [WHEEL_NAME, SDIST_NAME]


def test_report_records_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _layout(tmp_path)
    report = tmp_path / "verify-dist.json"
    code, _ = _run(tmp_path, capsys, "--report", str(report))
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["status"] == "pass"
    assert data["failures"] == []
    assert data["artifacts"][0]["facts"] == {
        "generator": "hatchling 1.29.0",
        "licenses/": "LICENSE",
        "METADATA License-File": "LICENSE",
    }
    assert data["artifacts"][1]["facts"] == {"PKG-INFO License-File": "LICENSE"}


# ------------------------------------------------------- review findings: input that once crashed


def test_non_ascii_metadata_values_are_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Parsed as bytes, a non-ASCII header value is an email.header.Header, not text,
    # and the report could neither sort nor serialise it.
    metadata = _metadata().replace(
        b"Version: 0.4.0",
        "Version: 0.4.0\nAuthor: Wolfgang Hörmann\nLicense-File: LICENCE-für-Daten.txt".encode(),
    )
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, metadata=metadata)
    _sdist(dist / SDIST_NAME, pkg_info=metadata)
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "METADATA License-File: LICENCE-für-Daten.txt, LICENSE" in out


def test_a_truncated_sdist_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _sdist(
        dist / SDIST_NAME, extra=tuple((f"{SDIST_ROOT}/f{i}", os.urandom(4000)) for i in range(20))
    )
    data = (dist / SDIST_NAME).read_bytes()
    (dist / SDIST_NAME).write_bytes(data[: len(data) // 2])
    report = tmp_path / "report.json"
    code, out = _run(tmp_path, capsys, "--report", str(report))
    assert code == 1
    assert f"{SDIST_NAME}: not a readable sdist" in out
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "fail"


def test_a_corrupt_wheel_member_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    with zipfile.ZipFile(dist / WHEEL_NAME, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("machina/__init__.py", b"")
        archive.writestr(f"{DIST_INFO}/METADATA", _metadata())
        archive.writestr(f"{DIST_INFO}/licenses/LICENSE", LICENSE * 200)
    data = bytearray((dist / WHEEL_NAME).read_bytes())
    start = data.find(f"{DIST_INFO}/licenses/LICENSE".encode()) + len(
        f"{DIST_INFO}/licenses/LICENSE"
    )
    for offset in range(start + 5, start + 25):
        data[offset] ^= 0xFF
    (dist / WHEEL_NAME).write_bytes(bytes(data))
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"{WHEEL_NAME}: not a readable wheel" in out


def test_a_crlf_reference_license_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # What a core.autocrlf=true checkout gives: CRLF in the reference and in both builds.
    crlf = LICENSE.replace(b"\n", b"\r\n")
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, license_bytes=crlf)
    _sdist(dist / SDIST_NAME, license_bytes=crlf)
    (tmp_path / "LICENSE").write_bytes(crlf)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "has CR line endings" in out


# ------------------------------------------------------ review findings: names and sdist members


@pytest.mark.parametrize(
    "member",
    [
        "machina/../vendored/blob.py",
        "machina/./x.py",
        "machina//x.py",
        # On Linux pip would install it as a top-level file named "machina\x.py".
        pytest.param(
            "machina\\x.py",
            marks=pytest.mark.skipif(
                os.sep == "\\", reason="zipfile reads a backslash as '/' on Windows"
            ),
        ),
        "/machina/x.py",
    ],
)
def test_wheel_member_names_that_step_around_the_checks_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], member: str
) -> None:
    dist = _layout(tmp_path)
    # A ZipInfo whose filename is set afterwards keeps it as given: on Windows,
    # zipfile would otherwise turn the backslash into "/".
    info = zipfile.ZipInfo("placeholder")
    info.filename = member
    with zipfile.ZipFile(dist / WHEEL_NAME, "a") as archive:
        archive.writestr(info, b"")
    assert member in zipfile.ZipFile(dist / WHEEL_NAME).namelist()
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "member names with" in out


def test_an_sdist_dot_segment_beside_the_real_license_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Duplicates are compared by raw name, so ./ would hide a second LICENSE.
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, extra=((f"{SDIST_ROOT}/./LICENSE", b"other\n"),))
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert "member names with" in out


def test_an_sdist_with_a_duplicate_member_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, extra=((f"{SDIST_ROOT}/LICENSE", b"other\n"),))
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"duplicate members: ['{SDIST_ROOT}/LICENSE']" in out


def test_an_sdist_without_pkg_info_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _sdist(dist / SDIST_NAME, pkg_info=None)
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"missing member {SDIST_ROOT}/PKG-INFO" in out


def test_other_files_under_licenses_are_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dist = _layout(tmp_path)
    _wheel(dist / WHEEL_NAME, extra={f"{DIST_INFO}/licenses/NOTICE": b"n\n"})
    code, out = _run(tmp_path, capsys)
    assert code == 0, out
    assert "licenses/: LICENSE, NOTICE" in out
