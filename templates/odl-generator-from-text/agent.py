#!/usr/bin/env python3
"""OdL Generator — free-text message to Work Order.

A technician sends a message (email or Telegram):
    "pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"

The agent parses the Italian text, resolves assets, creates structured
Work Orders, and replies with confirmation.

    cp .env.example .env   # fill in your LLM key
    docker compose up
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from workflows.parse_message_to_wo import message_to_workorder

from machina import Agent
from machina.observability.logging import configure_logging


def resolve_sandbox(
    *,
    sandbox_flag: bool,
    live_flag: bool,
    env_value: str | None,
) -> bool:
    """Compute sandbox mode for the template.

    Precedence: ``--live`` wins, then ``--sandbox``, then
    ``MACHINA_SANDBOX_MODE`` env var. The env var defaults to ``"true"``
    (sandbox-on) when unset, so a fresh container starts in sandbox;
    setting ``MACHINA_SANDBOX_MODE=false`` makes LIVE the implicit default.

    Kept as a free function (not a method) so unit tests can pin the
    precedence rule without instantiating an :class:`Agent`.
    """
    if live_flag:
        return False
    if sandbox_flag:
        return True
    return (env_value if env_value is not None else "true").lower() == "true"


def main() -> None:
    parser = argparse.ArgumentParser(description="OdL Generator from Text")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.yaml"),
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--sandbox",
        action="store_true",
        help="Sandbox mode — writes are logged, not executed",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Live mode — writes are executed (overrides MACHINA_SANDBOX_MODE)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else os.getenv("MACHINA_LOG_LEVEL", "INFO"))

    agent = Agent.from_config(args.config)
    agent.register_workflow(message_to_workorder)

    agent.sandbox = resolve_sandbox(
        sandbox_flag=args.sandbox,
        live_flag=args.live,
        env_value=os.getenv("MACHINA_SANDBOX_MODE"),
    )

    mode = "SANDBOX" if agent.sandbox else "LIVE"
    print(f"\n{'=' * 60}")
    print(f"  {agent.name}  |  Mode: {mode}")
    print(f"{'=' * 60}")
    print()
    print("  Send a message via email or Telegram:")
    print("  Italian:")
    print('    "pompa P-201 perde acqua, prego creare OdL"')
    print('    "caldaia C-3 rumore anomalo"')
    print("  English:")
    print('    "pump P-201 leaking water, please create WO"')
    print('    "boiler C-3 abnormal noise"')
    print()
    print("  Type 'quit' or Ctrl+C to exit.")
    print(f"{'=' * 60}\n")

    agent.run()


if __name__ == "__main__":
    main()
