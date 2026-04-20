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


def main() -> None:
    parser = argparse.ArgumentParser(description="OdL Generator from Text")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.yaml"),
    )
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else os.getenv("MACHINA_LOG_LEVEL", "INFO"))

    agent = Agent.from_config(args.config)
    agent.register_workflow(message_to_workorder)

    if args.sandbox or os.getenv("MACHINA_SANDBOX_MODE", "true").lower() == "true":
        agent.sandbox = True

    mode = "SANDBOX" if agent.sandbox else "LIVE"
    print(f"\n{'=' * 60}")
    print(f"  {agent.name}  |  Mode: {mode}")
    print(f"{'=' * 60}")
    print()
    print("  Send a message via email or Telegram:")
    print('    "pompa P-201 perde acqua, prego creare OdL"')
    print('    "caldaia C-3 rumore anomalo"')
    print()
    print("  Type 'quit' or Ctrl+C to exit.")
    print(f"{'=' * 60}\n")

    agent.run()


if __name__ == "__main__":
    main()
