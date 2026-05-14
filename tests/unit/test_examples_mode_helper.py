"""Unit tests for the shared CLI helpers used by example agents.

Pins the precedence rules of :func:`examples._mode.resolve_sandbox`
and the mutual-exclusion behaviour of :func:`examples._mode.add_mode_flags`
so a future signature drift surfaces here rather than at runtime in
each example.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

from _mode import add_mode_flags, resolve_sandbox  # noqa: E402


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_mode_flags(parser)
    return parser.parse_args(list(argv))


@pytest.mark.parametrize(
    ("argv", "default", "expected"),
    [
        # No flag — default wins.
        ([], True, True),
        ([], False, False),
        # --sandbox forces True regardless of default.
        (["--sandbox"], True, True),
        (["--sandbox"], False, True),
        # --live forces False regardless of default.
        (["--live"], True, False),
        (["--live"], False, False),
    ],
)
def test_resolve_sandbox_precedence(argv: list[str], default: bool, expected: bool) -> None:
    args = _parse(*argv)
    assert resolve_sandbox(args, default=default) is expected


def test_sandbox_and_live_are_mutually_exclusive() -> None:
    """argparse must reject ``--sandbox --live`` before resolve runs."""
    parser = argparse.ArgumentParser()
    add_mode_flags(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--sandbox", "--live"])


def _help_for_flag(parser: argparse.ArgumentParser, flag: str) -> str:
    """Read the help string registered for ``flag`` directly from the parser."""
    for action in parser._actions:
        if flag in action.option_strings:
            return action.help or ""
    raise AssertionError(f"{flag} is not registered on the parser")


def test_default_sandbox_annotates_help_text() -> None:
    """``default_sandbox=True`` annotates the sandbox flag's own help string."""
    parser = argparse.ArgumentParser()
    add_mode_flags(parser, default_sandbox=True)
    assert "(default)" in _help_for_flag(parser, "--sandbox")
    assert "(default)" not in _help_for_flag(parser, "--live")


def test_default_live_annotates_help_text() -> None:
    parser = argparse.ArgumentParser()
    add_mode_flags(parser, default_sandbox=False)
    assert "(default)" not in _help_for_flag(parser, "--sandbox")
    assert "(default)" in _help_for_flag(parser, "--live")


def test_no_default_label_means_no_annotation() -> None:
    parser = argparse.ArgumentParser()
    add_mode_flags(parser)
    assert "(default)" not in _help_for_flag(parser, "--sandbox")
    assert "(default)" not in _help_for_flag(parser, "--live")
