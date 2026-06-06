#!/usr/bin/env python3
"""Your first maintenance agent — 13 lines of Python.

pip install machina-ai[litellm]
ollama pull qwen2.5:3b
python agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
_examples_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(0, str(_examples_dir))

from _mode import add_mode_flags, resolve_sandbox  # noqa: E402
from _preflight import check  # noqa: E402

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

SAMPLE_DIR = _examples_dir / "sample_data"


# Small, CPU-runnable default. Substitute a newer Qwen you have pulled
# (e.g. "ollama:qwen3:4b") via --llm if you prefer.
def _build_agent(llm: str = "ollama:qwen2.5:3b", sandbox: bool = False) -> Agent:
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
# The literal definition lives in `_build_agent` above — that is the
# hero of this example. `main()` invokes it with CLI overrides; we
# deliberately avoid building a module-level instance so that
# `python agent.py --help` does not pay the connector construction cost.
# ────────────────────────────────────────────────────────────────


# -- Everything below is optional CLI convenience ----------------


def main() -> None:
    import argparse

    from machina.observability.logging import configure_logging

    parser = argparse.ArgumentParser(description="Machina Quickstart")
    parser.add_argument(
        "--llm",
        default="ollama:qwen2.5:3b",
        help="LLM provider:model (e.g. openai:gpt-4o, anthropic:claude-sonnet-4-20250514)",
    )

    add_mode_flags(parser, default_sandbox=False)
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else "INFO")
    check(llm=args.llm, sample_dir=SAMPLE_DIR)

    # Quickstart is read-mostly Q&A: default to LIVE so users can experiment freely.
    sandbox = resolve_sandbox(args, default=False)

    # Build agent with CLI overrides
    agent = _build_agent(llm=args.llm, sandbox=sandbox)

    mode = "SANDBOX" if sandbox else "LIVE"
    print(f"\n{'=' * 60}")
    print(f"  Machina Quickstart  |  LLM: {args.llm}  |  Mode: {mode}")
    print(f"{'=' * 60}")
    print()
    print("  Try asking:")
    print('    "What is the bearing replacement procedure for pump P-201?"')
    print('    "Are there spare bearings in stock?"')
    print('    "List all critical assets"')
    print('    "Create a work order for bearing replacement, priority HIGH"')
    print()
    print("  Type 'quit' or Ctrl+C to exit.")
    print(f"{'=' * 60}\n")

    agent.run()


if __name__ == "__main__":
    main()
