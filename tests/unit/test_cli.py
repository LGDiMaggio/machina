"""Unit tests for the ``machina`` CLI (``machina.cli``)."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_write_stdout_tolerates_narrow_console_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_write_stdout`` must not raise ``UnicodeEncodeError`` on a non-UTF-8
    console (e.g. a Windows cp1252/ascii terminal). The spine can carry an em
    dash or arrow; on a narrow stream those are replaced, not fatal.

    capsys always captures as UTF-8, so this path needs an explicit narrow
    stream to be exercised at all.
    """
    import io
    import sys

    class NarrowStdout(io.StringIO):
        encoding = "ascii"

    fake = NarrowStdout()
    monkeypatch.setattr(sys, "stdout", fake)

    cli._write_stdout("em—dash and arrow →\n")  # contains non-ASCII

    out = fake.getvalue()
    assert "dash" in out  # the ASCII text survived
    assert "?" in out  # the non-ASCII glyphs were replaced, not raised on


# ---------------------------------------------------------------------------
# machina mcp serve
# ---------------------------------------------------------------------------


@pytest.fixture()
def serve_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace ``machina.mcp.server.serve`` with a recorder (no server starts)."""
    import machina.mcp.server as mcp_server

    calls: list[dict[str, object]] = []

    def _fake_serve(config: object, **kwargs: object) -> None:
        calls.append({"config": config, **kwargs})

    monkeypatch.setattr(mcp_server, "serve", _fake_serve)
    return calls


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "machina.yaml"
    path.write_text('name: "CLI test"\n', encoding="utf-8")
    return path


def test_mcp_serve_delegates_with_defaults(
    serve_calls: list[dict[str, object]], config_file: Path
) -> None:
    """``machina mcp serve`` loads the config and calls ``serve`` with the defaults."""
    from machina.config.schema import MachinaConfig

    exit_code = cli.main(["mcp", "serve", "--config", str(config_file)])

    assert exit_code == 0
    assert len(serve_calls) == 1
    call = serve_calls[0]
    assert isinstance(call["config"], MachinaConfig)
    assert call["config"].name == "CLI test"
    assert call["transport"] == "stdio"
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8000


def test_mcp_serve_forwards_transport_options(
    serve_calls: list[dict[str, object]], config_file: Path
) -> None:
    exit_code = cli.main(
        [
            "mcp",
            "serve",
            "--config",
            str(config_file),
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
        ]
    )

    assert exit_code == 0
    call = serve_calls[0]
    assert call["transport"] == "streamable-http"
    assert call["host"] == "0.0.0.0"
    assert call["port"] == 9123


def test_mcp_serve_missing_config_returns_1(
    serve_calls: list[dict[str, object]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "nope.yaml"

    exit_code = cli.main(["mcp", "serve", "--config", str(missing)])

    assert exit_code == 1
    assert str(missing) in capsys.readouterr().err
    assert serve_calls == []


@pytest.mark.parametrize("argv", [["mcp"], ["mcp", "serve"]])
def test_mcp_serve_usage_errors_exit_2(argv: list[str]) -> None:
    """A missing nested subcommand or a missing --config is an argparse error."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


def test_python_m_machina_mcp_matches_cli(
    serve_calls: list[dict[str, object]], config_file: Path
) -> None:
    """``python -m machina.mcp`` and ``machina mcp serve`` share one argument set."""
    from machina.mcp.__main__ import main as mcp_main

    argv = ["--config", str(config_file), "--transport", "streamable-http", "--port", "9001"]
    cli.main(["mcp", "serve", *argv])
    mcp_main(argv)

    via_cli, via_module = serve_calls
    assert via_cli["config"] == via_module["config"]
    assert {k: v for k, v in via_cli.items() if k != "config"} == {
        k: v for k, v in via_module.items() if k != "config"
    }
