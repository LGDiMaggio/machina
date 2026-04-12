"""Tests for Agent.from_config() — YAML-driven agent construction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

from machina.agent.runtime import Agent


class TestFromConfig:
    """Verify Agent.from_config() creates a working agent."""

    def _write_yaml(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "machina.yaml"
        p.write_text(yaml.dump(data))
        return p

    def test_minimal_config(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "name": "Test Agent",
                "connectors": {
                    "cmms": {"type": "generic_cmms", "settings": {"data_dir": str(tmp_path)}},
                },
            },
        )
        agent = Agent.from_config(cfg_path)
        assert agent.name == "Test Agent"

    def test_plant_from_config(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "plant": {"name": "North Plant", "location": "Building A"},
            },
        )
        agent = Agent.from_config(cfg_path)
        assert agent.plant.name == "North Plant"
        assert agent.plant.location == "Building A"

    def test_sandbox_from_config(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(tmp_path, {"sandbox": True})
        agent = Agent.from_config(cfg_path)
        assert agent.sandbox is True

    def test_llm_from_config(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "llm": {"provider": "ollama:mistral", "temperature": 0.5},
            },
        )
        agent = Agent.from_config(cfg_path)
        assert agent._llm.model == "ollama:mistral"
        assert agent._llm.temperature == 0.5

    def test_disabled_connector_excluded(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "connectors": {
                    "active": {"type": "generic_cmms", "settings": {"data_dir": str(tmp_path)}},
                    "disabled": {
                        "type": "generic_cmms",
                        "enabled": False,
                        "settings": {"data_dir": str(tmp_path)},
                    },
                },
            },
        )
        agent = Agent.from_config(cfg_path)
        # Only one connector should be registered (the enabled one)
        assert len(agent._registry.all()) == 1

    def test_default_cli_channel_when_none_specified(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(tmp_path, {"name": "No Channels"})
        agent = Agent.from_config(cfg_path)
        assert len(agent._channels) == 1
        assert type(agent._channels[0]).__name__ == "CliChannel"

    def test_explicit_cli_channel(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "channels": [{"type": "cli"}],
            },
        )
        agent = Agent.from_config(cfg_path)
        assert len(agent._channels) == 1

    def test_workflows_registered_after_config(self, tmp_path: Path) -> None:
        cfg_path = self._write_yaml(tmp_path, {"name": "WF Agent"})
        agent = Agent.from_config(cfg_path)
        assert len(agent.workflows) == 0

        from machina.workflows.builtins import alarm_to_workorder

        agent.register_workflow(alarm_to_workorder)
        assert "Alarm to Work Order" in agent.workflows
