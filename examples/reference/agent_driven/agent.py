#!/usr/bin/env python3
"""Autonomous agent — no predefined workflows, pure tool-based reasoning.

The agent receives a complex maintenance scenario and decides what to do
by itself: which tools to call, in which order, and what action to take.

    pip install machina-ai[litellm,docs-rag]
    ollama pull llama3
    python agent.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.docs import DocumentStoreConnector

SAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "sample_data"

# The scenario: an operator reports a problem. The agent must figure out
# what to do — look up the asset, check history, diagnose, search manuals,
# verify spare parts, and propose (or create) a work order.
SCENARIO_IT = (
    "La pompa P-201 mostra vibrazioni anomale a 8.5 mm/s, "
    "ben sopra la soglia di allarme di 7.0 mm/s. "
    "Cosa dobbiamo fare? Verifica lo storico manutenzione, "
    "cerca nel manuale la procedura corretta, "
    "controlla se abbiamo ricambi disponibili, "
    "e se necessario crea un ordine di lavoro predittivo."
)
SCENARIO_EN = (
    "Pump P-201 shows abnormal vibrations at 8.5 mm/s, "
    "well above the alarm threshold of 7.0 mm/s. "
    "What should we do? Check the maintenance history, "
    "look up the correct procedure in the manual, "
    "verify if spare parts are available, "
    "and if necessary create a predictive work order."
)


def _build_agent(llm: str = "ollama:llama3", sandbox: bool = True) -> Agent:
    """Build the agent with connectors but no workflows."""
    return Agent(
        name="Autonomous Maintenance Agent",
        plant=Plant(name="Demo Plant"),
        connectors=[
            GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
            DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
        ],
        channels=[],  # No interactive channel — single-shot scenario
        llm=llm,
        sandbox=sandbox,
    )


async def run_scenario(agent: Agent, scenario: str) -> str:
    """Send a scenario to the agent and return its response."""
    await agent.start()
    try:
        response = await agent.handle_message(scenario, chat_id="demo")
        return response
    finally:
        await agent.stop()


def main() -> None:
    import argparse

    from machina.observability.logging import configure_logging

    parser = argparse.ArgumentParser(
        description="Autonomous maintenance agent — no workflows, pure reasoning",
    )
    parser.add_argument(
        "--llm",
        default="ollama:llama3",
        help="LLM provider:model (e.g. openai:gpt-4o, anthropic:claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        default=True,
        help="Enable sandbox mode — writes are logged, not executed (default: on)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Disable sandbox mode — writes are actually executed",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument(
        "--lang",
        choices=["it", "en"],
        default="it",
        help="Scenario language: 'it' for Italian (default), 'en' for English",
    )
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else "INFO")

    scenario = SCENARIO_EN if args.lang == "en" else SCENARIO_IT

    sandbox = not args.live

    # Pre-flight checks
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from _preflight import check

    check(llm=args.llm, sample_dir=SAMPLE_DIR)

    agent = _build_agent(llm=args.llm, sandbox=sandbox)
    mode = "SANDBOX" if sandbox else "LIVE"

    print(f"\n{'=' * 60}")
    print(f"  Agent-Driven Maintenance  |  LLM: {args.llm}  |  Mode: {mode}")
    print(f"{'=' * 60}")
    print()
    print("  Scenario:")
    print(f"  {scenario}")
    print()
    print(f"{'=' * 60}")
    print("  The agent will now reason autonomously...")
    print(f"{'=' * 60}\n")

    response = asyncio.run(run_scenario(agent, scenario))

    print(f"\n{'=' * 60}")
    print("  Agent Response:")
    print(f"{'=' * 60}\n")
    print(response)
    print()


if __name__ == "__main__":
    main()
