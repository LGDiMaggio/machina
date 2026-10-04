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
        assert agent._llm.model == "ollama/mistral"
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
        # Only one non-channel connector should be registered (the enabled one).
        # Channels are also registered into the same registry under keys prefixed
        # with "channel_" (see issue #31), so filter them out for this check.
        non_channel = {
            k: v for k, v in agent._registry.all().items() if not k.startswith("channel_")
        }
        assert len(non_channel) == 1

    async def test_excel_csv_substrate_from_yaml(self, tmp_path: Path) -> None:
        """The Excel/CSV substrate is buildable from YAML through Agent.from_config."""
        assets = tmp_path / "assets.csv"
        assets.write_text("Codice,Nome\nP-201,Pompa A\nC-3,Caldaia\n", encoding="utf-8")
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "connectors": {
                    "registry": {
                        "type": "excel_csv",
                        "settings": {
                            "asset_registry": {
                                "path": str(assets),
                                "columns": [
                                    {"column": "Codice", "field": "id", "required": True},
                                    {"column": "Nome", "field": "name", "required": True},
                                ],
                            }
                        },
                    }
                },
            },
        )
        agent = Agent.from_config(cfg_path)
        await agent.start()
        try:
            assert {a.id for a in agent.plant.assets.values()} == {"P-201", "C-3"}
        finally:
            await agent.stop()

    def test_runtime_and_agent_accept_the_same_excel_entry(self, tmp_path: Path) -> None:
        """MachinaRuntime (the MCP server path) builds the same YAML entry."""
        from machina.config.loader import load_config
        from machina.runtime import MachinaRuntime

        assets = tmp_path / "assets.csv"
        assets.write_text("Codice,Nome\nP-201,Pompa A\n", encoding="utf-8")
        cfg_path = self._write_yaml(
            tmp_path,
            {
                "connectors": {
                    "registry": {
                        "type": "excel_csv",
                        "settings": {
                            "asset_registry": {
                                "path": str(assets),
                                "columns": [{"column": "Codice", "field": "id", "required": True}],
                            }
                        },
                    }
                },
            },
        )
        runtime = MachinaRuntime.from_config(load_config(cfg_path))
        assert list(runtime.connectors) == ["registry"]

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
