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
from typing import TYPE_CHECKING, Any

# Ensure the src directory is importable when running from the examples folder
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant  # noqa: E402
from machina.connectors.cmms import GenericCmmsConnector  # noqa: E402
from machina.connectors.comms.telegram import CliChannel, TelegramConnector  # noqa: E402
from machina.connectors.docs import DocumentStoreConnector  # noqa: E402
from machina.observability.logging import configure_logging  # noqa: E402

if TYPE_CHECKING:
    from machina.llm.provider import LLMProvider


def build_agent(
    *,
    llm: str | LLMProvider = "openai:gpt-4o",
    use_telegram: bool = False,
    temperature: float = 0.1,
) -> Agent:
    """Construct the Knowledge Agent with sample data and connectors.

    Extracted from :func:`main` so tests (and other programmatic callers)
    can build an Agent without triggering argparse or the blocking
    :meth:`Agent.run` loop.

    Args:
        llm: Either a ``provider:model`` string (e.g. ``"openai:gpt-4o"``)
            or an :class:`LLMProvider`-compatible instance (useful for
            injecting stubs in tests).
        use_telegram: If True, use the Telegram channel instead of the
            interactive CLI channel. Requires ``TELEGRAM_BOT_TOKEN`` in
            the environment.
        temperature: LLM sampling temperature.

    Returns:
        A configured but not-yet-started :class:`Agent`.
    """
    sample_dir = Path(__file__).parent / "sample_data"

    # 1. Configure CMMS connector (local mode with sample data)
    cmms = GenericCmmsConnector(data_dir=sample_dir / "cmms")

    # 2. Configure Document Store (maintenance manuals)
    docs = DocumentStoreConnector(paths=[sample_dir / "manuals"])

    # 3. Configure communication channel
    channel: Any
    if use_telegram:
        import os

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN environment variable is required when use_telegram=True"
            )
        channel = TelegramConnector(bot_token=bot_token)
    else:
        channel = CliChannel()

    return Agent(
        name="Maintenance Knowledge Agent",
        description=(
            "Answers questions about plant equipment, maintenance history, "
            "procedures, spare parts, and failure diagnosis."
        ),
        plant=Plant(name="North Plant"),
        connectors=[cmms, docs],
        channels=[channel],
        llm=llm,
        temperature=temperature,
    )


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

    try:
        agent = build_agent(llm=args.llm, use_telegram=args.telegram)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    sample_dir = Path(__file__).parent / "sample_data"
    print("\n🔧 Starting Machina Knowledge Agent...")
    print(f"   LLM: {args.llm}")
    print(f"   Channel: {'Telegram' if args.telegram else 'CLI'}")
    print(f"   Sample data: {sample_dir}\n")

    agent.run()


if __name__ == "__main__":
    main()
