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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EXAMPLE_AGENTS = [
    REPO_ROOT / "examples" / "quickstart" / "agent.py",
    REPO_ROOT / "examples" / "alarm_to_workorder" / "agent.py",
    REPO_ROOT / "examples" / "reference" / "predictive_pipeline" / "agent.py",
    REPO_ROOT / "examples" / "reference" / "custom_workflows" / "agent.py",
    REPO_ROOT / "examples" / "reference" / "agent_driven" / "agent.py",
    REPO_ROOT / "examples" / "reference" / "yaml_config" / "agent.py",
    REPO_ROOT / "templates" / "odl-generator-from-text" / "agent.py",
]


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
    # argparse exits with code 2 and writes the error to stderr.
    assert result.returncode == 2, (
        f"{agent} accepted --sandbox --live together (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "not allowed with" in result.stderr or "mutually exclusive" in result.stderr, (
        f"{agent} rejected the combo but not for mutual exclusion:\n{result.stderr}"
    )
