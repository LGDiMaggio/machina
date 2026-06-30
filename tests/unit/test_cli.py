"""Unit tests for the ``machina`` CLI (``machina.cli``)."""

from __future__ import annotations

import json

import pytest

from machina import cli


def test_describe_text_output(capsys: pytest.CaptureFixture[str]) -> None:
    """``machina describe`` exits 0 and names a known connector + capability."""
    exit_code = cli.main(["describe"])
    assert exit_code == 0

    out = capsys.readouterr().out
    # A known connector type and a known capability appear in the text summary.
    assert "opcua" in out
    assert "browse_nodes" in out
    # Structural section headers are present.
    assert "Connectors" in out
    assert "Extension seams" in out


def test_describe_json_output_parses(capsys: pytest.CaptureFixture[str]) -> None:
    """``machina describe --json`` emits parseable JSON with the spine keys."""
    exit_code = cli.main(["describe", "--json"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out)
    assert "connectors" in payload
    assert "capabilities" in payload
    assert "seams" in payload
    assert "gaps" in payload

    # Each connector entry carries a typed capability list.
    assert isinstance(payload["connectors"], list)
    types = {c["type"] for c in payload["connectors"]}
    assert "opcua" in types
    for conn in payload["connectors"]:
        assert "capabilities" in conn


def test_describe_json_matches_render_json(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI JSON is identical to render_llms.render_json (the artifact form)."""
    from machina.introspect import describe
    from machina.introspect.render_llms import render_json

    exit_code = cli.main(["describe", "--json"])
    assert exit_code == 0

    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload == render_json(describe())


def test_unknown_subcommand_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown subcommand exits non-zero and prints usage to stderr."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["bogus"])
    assert excinfo.value.code != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_no_subcommand_exits_nonzero() -> None:
    """Invoking with no subcommand is an error (a subcommand is required)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code != 0
