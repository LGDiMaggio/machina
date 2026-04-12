#!/usr/bin/env python3
"""Your first maintenance agent — 13 lines of Python.

    pip install machina-ai[litellm]
    ollama pull llama3
    python agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def _build_agent(llm: str = "ollama:llama3", sandbox: bool = False) -> Agent:
    """Build the agent with the given LLM and sandbox settings."""
    return Agent(
        name="Maintenance Assistant",
        plant=Plant(name="Demo Plant"),
        connectors=[
            GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
            DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
        ],
        channels=[CliChannel()],
        llm=llm,
        sandbox=sandbox,
    )


# ── The entire agent (13 lines) ────────────────────────────────
agent = _build_agent()
# ────────────────────────────────────────────────────────────────


# -- Everything below is optional CLI convenience ----------------

def main() -> None:
    import argparse

    from machina.observability.logging import configure_logging

    parser = argparse.ArgumentParser(description="Machina Quickstart")
    parser.add_argument(
        "--llm", default="ollama:llama3",
        help="LLM provider:model (e.g. openai:gpt-4o, anthropic:claude-sonnet-4-20250514)",
    )
    parser.add_argument("--sandbox", action="store_true", help="Enable sandbox mode (writes are logged, not executed)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else "INFO")

    # Pre-flight: check sample data, LLM provider, and required extras
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _preflight import check
    check(llm=args.llm, sample_dir=SAMPLE_DIR)

    # Build agent with CLI overrides
    global agent
    agent = _build_agent(llm=args.llm, sandbox=args.sandbox)

    mode = "SANDBOX" if args.sandbox else "LIVE"
    print(f"\n{'='*60}")
    print(f"  Machina Quickstart  |  LLM: {args.llm}  |  Mode: {mode}")
    print(f"{'='*60}")
    print()
    print("  Try asking:")
    print('    "What is the bearing replacement procedure for pump P-201?"')
    print('    "Are there spare bearings in stock?"')
    print('    "List all critical assets"')
    print('    "Create a work order for bearing replacement, priority HIGH"')
    print()
    print("  Type 'quit' or Ctrl+C to exit.")
    print(f"{'='*60}\n")

    agent.run()


if __name__ == "__main__":
    main()
