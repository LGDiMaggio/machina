#!/usr/bin/env python3
"""Zero-code agent -- configure everything in YAML.

    python agent.py                          # uses machina.yaml
    python agent.py --config machina_openai.yaml
    python agent.py --llm openai:gpt-4o     # override LLM from CLI
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent


def main() -> None:
    parser = argparse.ArgumentParser(description="YAML-configured Machina Agent")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "machina.yaml"),
        help="Path to machina.yaml config file",
    )
    parser.add_argument("--llm", default=None, help="Override LLM provider:model")
    parser.add_argument("--sandbox", action="store_true", help="Enable sandbox mode")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Pre-flight checks
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _preflight import check

    # Build agent from YAML
    agent = Agent.from_config(args.config)

    # CLI overrides take precedence over YAML
    if args.llm:
        agent._llm = __import__("machina.llm.provider", fromlist=["LLMProvider"]).LLMProvider(model=args.llm)
    if args.sandbox:
        agent.sandbox = True

    check(llm=agent._llm.model)

    if args.verbose:
        from machina.observability.logging import configure_logging

        configure_logging(level="DEBUG")

    mode = "SANDBOX" if agent.sandbox else "LIVE"
    print(f"\n{'=' * 60}")
    print(f"  {agent.name}  |  Mode: {mode}")
    print(f"  Config: {args.config}")
    print(f"  LLM: {agent._llm.model}")
    print(f"{'=' * 60}")
    print()
    print("  Try asking:")
    print('    "What is the maintenance history of pump P-201?"')
    print('    "Which spare parts are in stock?"')
    print()
    print("  Type 'quit' or Ctrl+C to exit.")
    print(f"{'=' * 60}\n")

    agent.run()


if __name__ == "__main__":
    main()
