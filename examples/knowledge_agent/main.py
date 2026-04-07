#!/usr/bin/env python3
"""Machina Knowledge Agent — Quickstart Example.

This example creates a maintenance knowledge agent with:
- Sample CMMS data (assets, work orders, spare parts)
- Sample maintenance manuals (PDF/Markdown with RAG)
- CLI interaction mode (no Telegram needed)

Run it:
    cd examples/knowledge_agent
    python main.py

Or with Telegram:
    TELEGRAM_BOT_TOKEN=your_token python main.py --telegram
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the src directory is importable when running from the examples folder
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant  # noqa: E402
from machina.connectors.cmms import GenericCmmsConnector  # noqa: E402
from machina.connectors.comms.telegram import CliChannel, TelegramConnector  # noqa: E402
from machina.connectors.docs import DocumentStoreConnector  # noqa: E402
from machina.observability.logging import configure_logging  # noqa: E402


def main() -> None:
    """Run the Knowledge Agent quickstart."""
    parser = argparse.ArgumentParser(description="Machina Knowledge Agent")
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Use Telegram instead of CLI (requires TELEGRAM_BOT_TOKEN env var)",
    )
    parser.add_argument(
        "--llm",
        default="openai:gpt-4o",
        help="LLM provider:model (default: openai:gpt-4o)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    configure_logging(level="DEBUG" if args.verbose else "INFO")

    # Paths to sample data
    sample_dir = Path(__file__).parent / "sample_data"

    # 1. Configure CMMS connector (local mode with sample data)
    cmms = GenericCmmsConnector(data_dir=sample_dir / "cmms")

    # 2. Configure Document Store (maintenance manuals)
    docs = DocumentStoreConnector(paths=[sample_dir / "manuals"])

    # 3. Configure communication channel
    if args.telegram:
        import os

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            print("Error: TELEGRAM_BOT_TOKEN environment variable is required")  # noqa: T201
            sys.exit(1)
        channel = TelegramConnector(bot_token=bot_token)
    else:
        channel = CliChannel()

    # 4. Create and run the agent
    agent = Agent(
        name="Maintenance Knowledge Agent",
        description=(
            "Answers questions about plant equipment, maintenance history, "
            "procedures, spare parts, and failure diagnosis."
        ),
        plant=Plant(name="North Plant"),
        connectors=[cmms, docs],
        channels=[channel],
        llm=args.llm,
    )

    print("\n🔧 Starting Machina Knowledge Agent...")  # noqa: T201
    print(f"   LLM: {args.llm}")  # noqa: T201
    print(f"   Channel: {'Telegram' if args.telegram else 'CLI'}")  # noqa: T201
    print(f"   Sample data: {sample_dir}\n")  # noqa: T201

    agent.run()


if __name__ == "__main__":
    main()
