"""Shared CLI helpers for example agents.

All example scripts accept mutually exclusive ``--sandbox`` and ``--live``
flags with a consistent resolution rule.  Use :func:`add_mode_flags` to
register the flags and :func:`resolve_sandbox` to compute the boolean.

Example::

    parser = argparse.ArgumentParser(...)
    add_mode_flags(parser, default_sandbox=True)
    args = parser.parse_args()
    sandbox = resolve_sandbox(args, default=True)
"""

from __future__ import annotations

import argparse


def add_mode_flags(
    parser: argparse.ArgumentParser,
    *,
    default_sandbox: bool | None = None,
) -> None:
    """Register mutually exclusive ``--sandbox`` / ``--live`` flags.

    When ``default_sandbox`` is supplied, the help text for both flags
    annotates which one is the default. This lets ``--help`` reveal the
    default mode without requiring callers to read the source.
    """
    default_hint = ""
    if default_sandbox is True:
        default_hint = " (default)"
    elif default_sandbox is False:
        default_hint = ""

    live_hint = " (default)" if default_sandbox is False else ""

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--sandbox",
        action="store_true",
        help=f"Sandbox mode — writes are logged, not executed{default_hint}",
    )
    group.add_argument(
        "--live",
        action="store_true",
        help=f"Live mode — writes are executed{live_hint}",
    )


def resolve_sandbox(args: argparse.Namespace, *, default: bool) -> bool:
    """Resolve the sandbox boolean from parsed args.

    Precedence: ``--sandbox`` → True, ``--live`` → False, otherwise
    ``default``. The flags are registered in a mutually exclusive group
    by :func:`add_mode_flags`, so argparse rejects passing both before
    this function ever sees the namespace.
    """
    if args.live:
        return False
    if args.sandbox:
        return True
    return default
