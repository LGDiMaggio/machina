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
        ("PH210/1", "PH210%2F1"),
        ("wo1?limit=1#x", "wo1%3Flimit%3D1%23x"),
        ("50%", "50%25"),
        ("a b", "a%20b"),
        ("a..b", "a..b"),
        (".x/..y", ".x%2F..y"),
        ("WO;1", "WO%3B1"),
        (123, "123"),
    ],
)
def test_id_is_encoded_as_one_path_segment(record_id: object, segment: str) -> None:
    assert path_segment(record_id) == segment


@pytest.mark.parametrize(
    "record_id",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot-dot"),
        # A proxy that decodes %2F before normalizing the path would resolve
        # these into another resource, or into the collection itself.
        pytest.param("../users", id="leading-dot-dot-part"),
        pytest.param("a/../b", id="inner-dot-dot-part"),
        pytest.param("a/.", id="trailing-dot-part"),
        pytest.param("..\\x", id="backslash-separated-dot-dot-part"),
        pytest.param("/", id="separator-only"),
        pytest.param("a//b", id="empty-inner-part"),
        pytest.param("a/", id="empty-trailing-part"),
        # A servlet container drops ";" path parameters before resolving dot
        # segments, so "..;" behind a decoding proxy is "..".
        pytest.param("..;", id="dot-dot-with-path-parameter"),
        pytest.param("a/..;x/b", id="inner-dot-dot-with-path-parameter"),
        pytest.param(".;x", id="dot-with-path-parameter"),
        pytest.param(";x", id="empty-before-path-parameter"),
    ],
)
def test_id_that_could_leave_its_segment_is_refused(record_id: str) -> None:
    with pytest.raises(ConnectorError) as exc_info:
        path_segment(record_id)
    # One fixed message: it names the rule, never the caller's ID.
    assert str(exc_info.value) == (
        "Invalid record ID: an ID cannot be empty, and no part of it between "
        "'/' or '\\' separators can be empty, '.' or '..', including before a ';'"
    )
