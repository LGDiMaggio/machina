"""Integrity test for the project ``LICENSE``.

``LICENSE`` must be the canonical Apache License 2.0 text published at
https://www.apache.org/licenses/LICENSE-2.0.txt, appendix included, with only the
appendix's ``Copyright [yyyy] [name of copyright owner]`` placeholder filled in.
The test stores the SHA-256 of the canonical file, never its text, and reads the
copyright line from ``LICENSE`` instead of naming the holder.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_LICENSE = Path(__file__).resolve().parents[2] / "LICENSE"

# SHA-256 of https://www.apache.org/licenses/LICENSE-2.0.txt, fetched 2026-10-23.
_CANONICAL_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
_PLACEHOLDER = "   Copyright [yyyy] [name of copyright owner]"
_COPYRIGHT_LINE = re.compile(r"^\s*Copyright \S")


def test_license_is_the_canonical_apache_text() -> None:
    """Only the appendix's copyright line may differ from the apache.org file."""
    # read_text's universal newlines turn a Windows checkout's CRLF (core.autocrlf=true)
    # into LF, as in the canonical file.
    lines = _LICENSE.read_text(encoding="utf-8").split("\n")
    copyright_lines = [i for i, line in enumerate(lines) if _COPYRIGHT_LINE.match(line)]
    assert len(copyright_lines) == 1, f"expected one copyright line, found {len(copyright_lines)}"

    # LICENSE holds the project's only copyright notice: a re-pasted apache.org file,
    # placeholder unfilled, must fail rather than pass the hash check below.
    holder = copyright_lines[0]
    assert lines[holder] != _PLACEHOLDER, "LICENSE has the unfilled appendix placeholder"
    lines[holder] = _PLACEHOLDER
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    assert digest == _CANONICAL_SHA256, "LICENSE differs from the canonical Apache-2.0 text"
