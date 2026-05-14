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

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is in the [examples] extra; absence is fine.
    load_dotenv = None  # type: ignore[assignment]

EXAMPLES_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = EXAMPLES_DIR / "sample_data"


def check(*, llm: str = "ollama:llama3", sample_dir: Path | None = None) -> None:
    """Run all pre-flight checks and exit with a helpful message on failure.

    Args:
        llm: The ``provider:model`` string the user selected.
        sample_dir: Override for the sample data directory (defaults
            to ``examples/sample_data/``).
    """
    if load_dotenv is not None:
        load_dotenv(EXAMPLES_DIR / ".env")
    sample = sample_dir or SAMPLE_DIR
    _check_sample_data(sample)
    _check_llm(llm)


def _err(*args: object) -> None:
    """Print a preflight error line to stderr."""
    print(*args, file=sys.stderr)


def _check_sample_data(sample_dir: Path) -> None:
    if not sample_dir.exists():
        _err(f"Error: sample data not found at {sample_dir}")
        _err("Make sure you are running from the repo root or examples/ directory.")
        sys.exit(1)


def _check_llm(llm: str) -> None:
    provider = llm.split(":")[0] if ":" in llm else llm

    if provider == "ollama":
        if not shutil.which("ollama"):
            _err("Error: Ollama is not installed.")
            _err("Install it from https://ollama.com, then run:")
            _err(f"  ollama pull {llm.split(':', 1)[1] if ':' in llm else 'llama3'}")
            sys.exit(1)
        try:
            subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            _err("Error: Ollama is installed but does not seem to be running.")
            _err("Start it with: ollama serve")
            sys.exit(1)

    elif provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            _err("Error: OPENAI_API_KEY environment variable is not set.")
            _err("Get your API key from https://platform.openai.com/api-keys")
            _print_env_var_hint("OPENAI_API_KEY", "sk-...")
            sys.exit(1)

    elif provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            _err("Error: ANTHROPIC_API_KEY environment variable is not set.")
            _err("Get your API key from https://console.anthropic.com/")
            _print_env_var_hint("ANTHROPIC_API_KEY", "sk-ant-...")
            sys.exit(1)


def _print_env_var_hint(var: str, example: str) -> None:
    """Print the platform-appropriate command to set an environment variable."""
    if sys.platform == "win32":
        _err("Then run (PowerShell):")
        _err(f'  $env:{var} = "{example}"')
        _err("Or (CMD):")
        _err(f"  set {var}={example}")
    else:
        _err(f"Then run:  export {var}={example}")
