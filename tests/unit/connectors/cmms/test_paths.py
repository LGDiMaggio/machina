"""Unit tests for the CMMS REST URL-path helper."""

from __future__ import annotations

import pytest

from machina.connectors.cmms.paths import path_segment
from machina.exceptions import ConnectorError


@pytest.mark.parametrize(
    ("record_id", "segment"),
    [
        ("a1", "a1"),
        ("PUMP-201_A.1~x", "PUMP-201_A.1~x"),
        ("../users", "..%2Fusers"),
        ("wo1?limit=1#x", "wo1%3Flimit%3D1%23x"),
        ("50%", "50%25"),
        ("a b", "a%20b"),
        (123, "123"),
    ],
)
def test_id_is_encoded_as_one_path_segment(record_id: object, segment: str) -> None:
    assert path_segment(record_id) == segment


@pytest.mark.parametrize("record_id", ["", ".", ".."])
def test_empty_and_dot_segment_ids_are_refused(record_id: str) -> None:
    with pytest.raises(ConnectorError, match="Invalid record ID"):
        path_segment(record_id)
