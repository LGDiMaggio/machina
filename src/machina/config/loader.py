"""Configuration loader — YAML files with ``${ENV_VAR}`` substitution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from machina.config.schema import MachinaConfig

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _substitute_env_vars(value: str) -> str:
    """Replace ``${VAR}`` placeholders with environment variable values."""

    def _replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
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
    with path.open() as f:
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
