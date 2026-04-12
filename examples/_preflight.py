"""Pre-flight checks shared by all example scripts.

Verifies that sample data exists, the selected LLM provider is
reachable, and required Python extras are installed.  Call
:func:`check` at the top of ``main()`` before building the agent.

Usage::

    from _preflight import check
    check(llm=args.llm)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"


def check(*, llm: str = "ollama:llama3", sample_dir: Path | None = None) -> None:
    """Run all pre-flight checks and exit with a helpful message on failure.

    Args:
        llm: The ``provider:model`` string the user selected.
        sample_dir: Override for the sample data directory (defaults
            to ``examples/sample_data/``).
    """
    sample = sample_dir or SAMPLE_DIR
    _check_sample_data(sample)
    _check_llm(llm)


def _check_sample_data(sample_dir: Path) -> None:
    if not sample_dir.exists():
        print(f"Error: sample data not found at {sample_dir}")
        print("Make sure you are running from the repo root or examples/ directory.")
        sys.exit(1)


def _check_llm(llm: str) -> None:
    provider = llm.split(":")[0] if ":" in llm else llm

    if provider == "ollama":
        if not shutil.which("ollama"):
            print("Error: Ollama is not installed.")
            print("Install it from https://ollama.com, then run:")
            print(f"  ollama pull {llm.split(':', 1)[1] if ':' in llm else 'llama3'}")
            sys.exit(1)
        try:
            subprocess.run(
                ["ollama", "list"], capture_output=True, timeout=5, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            print("Error: Ollama is installed but does not seem to be running.")
            print("Start it with: ollama serve")
            sys.exit(1)

    elif provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            print("Error: OPENAI_API_KEY environment variable is not set.")
            print("Get your API key from https://platform.openai.com/api-keys")
            print("Then run:  export OPENAI_API_KEY=sk-...")
            sys.exit(1)

    elif provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("Error: ANTHROPIC_API_KEY environment variable is not set.")
            print("Get your API key from https://console.anthropic.com/")
            print("Then run:  export ANTHROPIC_API_KEY=sk-ant-...")
            sys.exit(1)
