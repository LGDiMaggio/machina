"""CLI consistency smoke tests for example agents.

Every example agent script must expose mutually exclusive ``--sandbox``
and ``--live`` flags so users learn one convention.  This test runs
``python <agent.py> --help`` for each known example and asserts the
flags are present.  It also asserts that passing both flags together
is rejected by argparse (mutual exclusion).

The test does NOT execute the agents — only their ``--help`` and
``--sandbox --live`` parsing — so it stays fast and offline.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# python-dotenv is in the `examples` extra; without it the example agents
# cannot import _preflight.  Skip the whole module rather than report
# spurious flag-missing failures.
pytest.importorskip("dotenv")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _discover_agents() -> list[Path]:
    """Find every ``agent.py`` under ``examples/`` and ``templates/``.

    Auto-discovery means a new example added in the future is covered
    without manually editing this test — preventing the kind of silent
    drift the test was created to prevent.
    """
    roots = [REPO_ROOT / "examples", REPO_ROOT / "templates"]
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        found.extend(p for p in root.rglob("agent.py") if "__pycache__" not in p.parts)
    return sorted(found)


EXAMPLE_AGENTS = _discover_agents()

assert EXAMPLE_AGENTS, "No example agent.py files found — discovery glob is broken."


def _run(agent: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(agent), *extra],
        capture_output=True,
        text=True,
        cwd=str(agent.parent),
        timeout=30,
    )


@pytest.mark.parametrize("agent", EXAMPLE_AGENTS, ids=lambda p: p.parent.name)
def test_example_exposes_sandbox_and_live_flags(agent: Path) -> None:
    """``--help`` must list both ``--sandbox`` and ``--live``."""
    result = _run(agent, "--help")
    assert result.returncode == 0, (
        f"{agent} --help exited {result.returncode}\nstderr:\n{result.stderr}"
    )
    help_text = result.stdout
    assert "--sandbox" in help_text, f"{agent} is missing --sandbox\n{help_text}"
    assert "--live" in help_text, f"{agent} is missing --live\n{help_text}"


@pytest.mark.parametrize("agent", EXAMPLE_AGENTS, ids=lambda p: p.parent.name)
def test_sandbox_and_live_are_mutually_exclusive(agent: Path) -> None:
    """Passing ``--sandbox --live`` together must be rejected by argparse."""
    result = _run(agent, "--sandbox", "--live")
    # argparse exits with code 2 and writes the error to stderr.  We rely on
    # the exit code as the behavioural signal (locale-independent) and only
    # use stderr text as a debugging aid in the failure message.
    assert result.returncode == 2, (
        f"{agent} accepted --sandbox --live together (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
