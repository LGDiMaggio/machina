#!/usr/bin/env python3
"""OdL Generator — free-text maintenance request to Work Order.

A technician types (or sends) a request such as:
    "pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"

The agent resolves the assets named in the message against the plant
registry (data/asset_registry.xlsx) and proposes one work order per asset.
In sandbox mode (the default) nothing is written; in live mode each work
order is confirmed before it is appended to data/workorders.xlsx.

    pip install "machina-ai[excel,litellm]"
    cp .env.example .env        # set your LLM key (or use a local Ollama model)
    python agent.py --sandbox
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(TEMPLATE_DIR.parent.parent / "src"))

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
    ``MACHINA_SANDBOX_MODE`` env var. Only an explicit false value
    (``false``, ``0``, ``no``, ``off``) selects LIVE; unset, blank and any
    other value keep sandbox on, so a fresh container — or a typo — starts
    in sandbox.

    Kept as a free function (not a method) so unit tests can pin the
    precedence rule without instantiating an :class:`Agent`.
    """
    if live_flag:
        return False
    if sandbox_flag:
        return True
    return (env_value or "").strip().lower() not in {"false", "0", "no", "off"}


def _load_dotenv() -> None:
    """Load ``.env`` next to this script when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(TEMPLATE_DIR / ".env")


def main() -> None:
    parser = argparse.ArgumentParser(description="OdL Generator from Text")
    parser.add_argument(
        "--config",
        default=str(TEMPLATE_DIR / "config.yaml"),
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
    config_path = Path(args.config).resolve()

    _load_dotenv()
    configure_logging(level="DEBUG" if args.verbose else os.getenv("MACHINA_LOG_LEVEL", "INFO"))

    # The data paths in config.yaml are relative to the template directory.
    os.chdir(TEMPLATE_DIR)
    agent = Agent.from_config(config_path)
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
    print("  Describe the problem; name the asset by code or name:")
    print('    "pompa P-201 perde acqua, caldaia C-3 rumore anomalo"')
    print('    "pump P-201 leaking water, boiler C-3 abnormal noise"')
    print()
    if agent.sandbox:
        print("  Sandbox: work orders are proposed and logged, not written.")
    else:
        print("  Live: each work order is confirmed before it is written to")
        print("  data/workorders.xlsx.")
    print("  Type 'quit' or Ctrl+C to exit.")
    print(f"{'=' * 60}\n")

    agent.run()


if __name__ == "__main__":
    main()
