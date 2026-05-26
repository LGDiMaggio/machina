"""Parity tests between the template's inline sandbox resolver and
the shared examples helper.

The odl-generator template must stay self-contained (clone-and-deploy),
so it duplicates the precedence rule from ``examples/_mode.py``. These
tests guard against the two implementations drifting silently — when
neither ``--sandbox`` nor ``--live`` is passed and the env var is
absent, both must default to SANDBOX. When a flag is set, both must
honour it identically.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "examples"))
sys.path.insert(0, str(REPO_ROOT / "templates" / "odl-generator-from-text"))

from _mode import add_mode_flags  # noqa: E402
from _mode import resolve_sandbox as resolve_examples  # noqa: E402
from agent import resolve_sandbox as resolve_template  # noqa: E402


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_mode_flags(parser)
    parser.parse_args(list(argv))  # would raise if mutex violated
    return parser.parse_args(list(argv))


@pytest.mark.parametrize(
    ("argv", "env_value"),
    [
        # No flag, env unset → both default to SANDBOX.
        ([], None),
        # No flag, env="true" → SANDBOX in both.
        ([], "true"),
        # No flag, env="false" → LIVE in both.
        ([], "false"),
        # --sandbox → SANDBOX regardless of env.
        (["--sandbox"], None),
        (["--sandbox"], "false"),
        # --live → LIVE regardless of env.
        (["--live"], None),
        (["--live"], "true"),
    ],
)
def test_template_matches_examples_helper(argv: list[str], env_value: str | None) -> None:
    """When the env var would not flip the result, the two resolvers agree."""
    args = _parse(*argv)

    # Map examples helper to the same domain: env_value="true"/None → default=True,
    # env_value="false" → default=False.  This is the contract the template encodes.
    examples_default = (env_value if env_value is not None else "true").lower() == "true"

    expected_examples = resolve_examples(args, default=examples_default)
    expected_template = resolve_template(
        sandbox_flag=args.sandbox,
        live_flag=args.live,
        env_value=env_value,
    )

    assert expected_examples == expected_template, (
        f"drift: argv={argv} env_value={env_value!r} "
        f"examples={expected_examples} template={expected_template}"
    )
