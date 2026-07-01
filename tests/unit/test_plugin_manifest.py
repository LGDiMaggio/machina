"""Manifest + integrity tests for the ``machina-agent-builder`` Claude Code plugin.

The plugin (Form C of the self-description spine) carries no runtime logic, so
its tests guard *integrity*, not behaviour: the manifest is valid, every command
it ships exists and is well-formed, and — crucially — the commands do NOT hardcode
a capability list. The plugin's whole value is that it reads the live spine
(``machina describe`` / ``docs/capabilities.md``) rather than copying capabilities
into prose that would drift from the code.
"""

from __future__ import annotations

import json
from pathlib import Path

from machina.connectors.capabilities import Capability

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "plugin"
_MANIFEST = _PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
_COMMANDS_DIR = _PLUGIN_ROOT / "commands"


def _command_files() -> list[Path]:
    return sorted(_COMMANDS_DIR.glob("*.md"))


def test_manifest_is_valid() -> None:
    """plugin.json parses and carries the required string fields."""
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "machina-agent-builder"
    for field in ("name", "version", "description"):
        assert isinstance(data[field], str) and data[field].strip(), field


def test_ships_at_least_one_command() -> None:
    assert _command_files(), "plugin ships no commands"


def test_commands_have_frontmatter_with_description() -> None:
    """Each command is a well-formed Claude Code command (frontmatter + description)."""
    for cmd in _command_files():
        text = cmd.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{cmd.name} missing YAML frontmatter"
        # frontmatter closes with a second '---' and carries a description.
        _, frontmatter, _body = text.split("---", 2)
        assert "description:" in frontmatter, f"{cmd.name} frontmatter lacks description"


def test_commands_read_the_live_spine() -> None:
    """Every command instructs reading the live spine, not a copied snapshot."""
    for cmd in _command_files():
        body = cmd.read_text(encoding="utf-8")
        assert "machina describe" in body, (
            f"{cmd.name} does not point at the live spine (`machina describe`)"
        )


def test_commands_do_not_hardcode_a_capability_list() -> None:
    """Guard against drift-by-copy: commands must delegate to the spine, not
    enumerate capability wire-strings. A command that lists capabilities inline
    would silently go stale when the Capability enum changes — exactly the failure
    the spine exists to prevent."""
    cap_values = [c.value for c in Capability]
    for cmd in _command_files():
        body = cmd.read_text(encoding="utf-8")
        hardcoded = sorted(v for v in cap_values if v in body)
        assert len(hardcoded) <= 1, (
            f"{cmd.name} appears to hardcode capability values {hardcoded} — "
            "commands must read them from `machina describe` / docs/capabilities.md instead"
        )
