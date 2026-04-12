#!/usr/bin/env python3
"""Same agent, any CMMS -- switch backends by changing one line.

SAP PM, IBM Maximo, UpKeep, or any REST-based CMMS. Your agent logic,
workflows, and prompts stay identical.

    python agent.py                     # GenericCmms (sample data)
    python agent.py --backend sap       # show SAP PM config
    python agent.py --backend maximo    # show Maximo config
    python agent.py --interactive       # chat with the agent
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"

# ── Configuration for each CMMS backend ─────────────────────────
#
# In production, pick one:
#
#   from machina.connectors import SapPM
#   cmms = SapPM(
#       url="https://sap.yourcompany.com/odata/v4",
#       auth=OAuth2ClientCredentials(
#           token_url="https://sap.yourcompany.com/oauth/token",
#           client_id=os.environ["SAP_CLIENT_ID"],
#           client_secret=os.environ["SAP_CLIENT_SECRET"],
#       ),
#   )
#
#   from machina.connectors import Maximo
#   cmms = Maximo(
#       url="https://maximo.yourcompany.com/maximo/oslc",
#       auth=ApiKeyHeaderAuth(header="apikey", value=os.environ["MAXIMO_API_KEY"]),
#   )
#
#   from machina.connectors import UpKeep
#   cmms = UpKeep(
#       url="https://api.onupkeep.com/api/v2",
#       auth=BearerAuth(token=os.environ["UPKEEP_API_TOKEN"]),
#   )
#
# Then pass it to Agent(connectors=[cmms, ...]) -- everything else
# stays the same.

BACKEND_CONFIGS = {
    "sap": (
        "from machina.connectors import SapPM\n"
        "cmms = SapPM(\n"
        '    url="https://sap.yourcompany.com/odata/v4",\n'
        "    auth=OAuth2ClientCredentials(\n"
        '        token_url="https://sap.yourcompany.com/oauth/token",\n'
        '        client_id=os.environ["SAP_CLIENT_ID"],\n'
        '        client_secret=os.environ["SAP_CLIENT_SECRET"],\n'
        "    ),\n"
        ")"
    ),
    "maximo": (
        "from machina.connectors import Maximo\n"
        "cmms = Maximo(\n"
        '    url="https://maximo.yourcompany.com/maximo/oslc",\n'
        '    auth=ApiKeyHeaderAuth(header="apikey", value=os.environ["MAXIMO_API_KEY"]),\n'
        ")"
    ),
    "upkeep": (
        "from machina.connectors import UpKeep\n"
        "cmms = UpKeep(\n"
        '    url="https://api.onupkeep.com/api/v2",\n'
        '    auth=BearerAuth(token=os.environ["UPKEEP_API_TOKEN"]),\n'
        ")"
    ),
    "generic": (
        "from machina.connectors import GenericCmms\n"
        'cmms = GenericCmms(data_dir="path/to/cmms/json/files")'
    ),
}


def build_agent(backend: str = "generic", llm: str = "ollama:llama3") -> Agent:
    """Build agent -- show the selected backend config, run with GenericCmms."""
    cmms = GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms")
    docs = DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"])

    return Agent(
        name="CMMS Portability Agent",
        plant=Plant(name="Portable Plant"),
        connectors=[cmms, docs],
        channels=[CliChannel()],
        llm=llm,
    )


async def run_demo(backend: str, llm: str) -> None:
    """Ask the same question regardless of CMMS backend."""
    print(f"\n  Configuration for {backend}:\n")
    print(f"    {BACKEND_CONFIGS.get(backend, BACKEND_CONFIGS['generic'])}\n")
    if backend != "generic":
        print("  (Using GenericCmms with sample data for the demo)\n")

    agent = build_agent(backend=backend, llm=llm)
    await agent.start()

    question = "List all critical assets and tell me if any have open work orders."
    print(f"  You: {question}\n")
    answer = await agent.handle_message(question)
    print(f"  Agent: {answer}\n")
    await agent.stop()

    print("  " + "-" * 58)
    print("  THE KEY INSIGHT:")
    print("  Your agent, workflows, and prompts stay identical.")
    print("  Only the connector changes:")
    print()
    print("    # cmms = SapPM(url=..., auth=...)    # client A")
    print("    # cmms = Maximo(url=..., auth=...)    # client B")
    print("    # cmms = UpKeep(url=..., auth=...)    # client C")
    print()
    print("    agent = Agent(connectors=[cmms, docs], ...)")
    print("    agent.run()  # identical")
    print("  " + "-" * 58 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="CMMS Portability Demo")
    parser.add_argument("--backend", choices=["generic", "sap", "maximo", "upkeep"],
                        default="generic", help="CMMS backend to show")
    parser.add_argument("--llm", default="ollama:llama3", help="LLM provider:model")
    parser.add_argument("--interactive", action="store_true", help="Interactive chat mode")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        from machina.observability.logging import configure_logging
        configure_logging(level="DEBUG")

    if args.interactive:
        agent = build_agent(backend=args.backend, llm=args.llm)
        agent.run()
    else:
        asyncio.run(run_demo(backend=args.backend, llm=args.llm))


if __name__ == "__main__":
    main()
