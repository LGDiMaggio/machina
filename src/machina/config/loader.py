"""Configuration loader — YAML files with ``${ENV_VAR}`` substitution.

Placeholders:

* ``${VAR}`` — the value of ``VAR``; loading fails if it is not set.
* ``${VAR:-default}`` — the value of ``VAR``, or ``default`` when ``VAR`` is
  unset or empty (POSIX shell / Docker Compose semantics). Defaults are meant
  for non-secret settings: a forgotten secret should fail loudly, not fall
  back.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from machina.config.schema import MachinaConfig

# Group 1: the variable name — anything up to ``}`` except the ``:-``
# separator, so a malformed placeholder such as ``${A:B}`` still fails as an
# unset variable instead of passing through as literal text.
# Group 2 (optional): the ``:-`` default.
_ENV_VAR_PATTERN = re.compile(r"\$\{((?:[^}:]|:(?!-))+)(?::-([^}]*))?\}")


def _substitute_env_vars(value: str) -> str:
    """Replace ``${VAR}`` / ``${VAR:-default}`` placeholders with values."""

    def _replacer(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        env_value = os.environ.get(var_name)
        if default is not None:
            return env_value if env_value else default
        if env_value is None:
            msg = f"Environment variable {var_name!r} is not set"
            raise ValueError(msg)
        return env_value

    return _ENV_VAR_PATTERN.sub(_replacer, value)


def _walk_and_substitute(obj: Any) -> Any:
    """Recursively substitute env vars in a nested dict/list structure."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _walk_and_substitute(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_substitute(item) for item in obj]
    return obj


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and perform environment variable substitution.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Parsed configuration dictionary with env vars resolved.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If a referenced env var is not set.
    """
    path = Path(path)
    # Always UTF-8: the platform default (cp1252 on Windows) would garble
    # non-ASCII values such as a column header "Criticità".
    with path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    result: dict[str, Any] = _walk_and_substitute(raw)
    return result


def load_config(path: str | Path) -> MachinaConfig:
    """Load and validate a Machina configuration file.

    Args:
        path: Path to ``machina.yaml``.

    Returns:
        A validated ``MachinaConfig`` instance.
    """
    data = load_yaml(path)
    return MachinaConfig.model_validate(data)
