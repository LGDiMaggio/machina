"""Shared CLI helpers for example agents.

All example scripts accept mutually exclusive ``--sandbox`` and ``--live``
flags with a consistent resolution rule.  Use :func:`add_mode_flags` to
register the flags and :func:`resolve_sandbox` to compute the boolean.

Example::

    parser = argparse.ArgumentParser(...)
    add_mode_flags(parser)
    args = parser.parse_args()
    sandbox = resolve_sandbox(args, default=True)
"""

from __future__ import annotations

import argparse


def add_mode_flags(parser: argparse.ArgumentParser) -> None:
    """Register mutually exclusive ``--sandbox`` / ``--live`` flags."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--sandbox",
        action="store_true",
        help="Sandbox mode — writes are logged, not executed",
    )
    group.add_argument(
        "--live",
        action="store_true",
        help="Live mode — writes are executed",
    )


def resolve_sandbox(args: argparse.Namespace, *, default: bool) -> bool:
    """Resolve the sandbox boolean from parsed args.

    ``--live`` wins over ``--sandbox`` (argparse guarantees they cannot
    both be set since the flags are in a mutually exclusive group).
    If neither flag is set, ``default`` is returned.
    """
    if args.live:
        return False
    if args.sandbox:
        return True
    return default
