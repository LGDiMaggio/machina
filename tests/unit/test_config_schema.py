"""Tests for the configuration schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from machina.config.schema import (
    ChannelConfig,
    ConnectorConfig,
    LLMConfig,
    MachinaConfig,
    PlantConfig,
)


class TestConnectorConfig:
    """Test ConnectorConfig validation."""

    def test_minimal_connector(self) -> None:
        cfg = ConnectorConfig(type="sap_pm")
        assert cfg.type == "sap_pm"
        assert cfg.enabled is True
        assert cfg.settings == {}

    def test_connector_with_settings(self) -> None:
        cfg = ConnectorConfig(
            type="telegram",
            settings={"bot_token": "tok-123", "chat_id": -100},
        )
        assert cfg.settings["bot_token"] == "tok-123"

    def test_connector_disabled(self) -> None:
        cfg = ConnectorConfig(type="opcua", enabled=False)
        assert cfg.enabled is False

    def test_connector_allows_extra_fields(self) -> None:
        cfg = ConnectorConfig(type="custom", custom_field="value")
        assert cfg.custom_field == "value"  # type: ignore[attr-defined]

    def test_connector_type_required(self) -> None:
        with pytest.raises(ValidationError):
            ConnectorConfig()  # type: ignore[call-arg]


class TestLLMConfig:
    """Test LLMConfig validation."""

    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.provider == "ollama:llama3"
        assert cfg.temperature == 0.1
        assert cfg.max_tokens == 4096

    def test_custom_provider(self) -> None:
        cfg = LLMConfig(provider="ollama:llama3", temperature=0.7)
        assert cfg.provider == "ollama:llama3"
        assert cfg.temperature == 0.7

    def test_temperature_bounds(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(temperature=-0.1)
        with pytest.raises(ValidationError):
            LLMConfig(temperature=2.1)

    def test_temperature_edge_values(self) -> None:
        assert LLMConfig(temperature=0).temperature == 0
        assert LLMConfig(temperature=2).temperature == 2

    def test_max_tokens_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            LLMConfig(max_tokens=0)

    def test_allows_extra_fields(self) -> None:
        cfg = LLMConfig(api_base="http://localhost:11434")
        assert cfg.api_base == "http://localhost:11434"  # type: ignore[attr-defined]


class TestMachinaConfig:
    """Test top-level MachinaConfig validation."""

    def test_defaults(self) -> None:
        cfg = MachinaConfig()
        assert cfg.name == "Machina Agent"
        assert cfg.connectors == {}
        assert isinstance(cfg.llm, LLMConfig)
        assert cfg.logging == {}

    def test_full_config(self) -> None:
        cfg = MachinaConfig(
            name="My Bot",
            connectors={
                "cmms": ConnectorConfig(type="sap_pm"),
                "telegram": ConnectorConfig(type="telegram", settings={"bot_token": "x"}),
            },
            llm=LLMConfig(provider="anthropic:claude-sonnet", temperature=0.3),
            logging={"level": "DEBUG", "json_output": True},
        )
        assert cfg.name == "My Bot"
        assert len(cfg.connectors) == 2
        assert cfg.llm.provider == "anthropic:claude-sonnet"
        assert cfg.logging["level"] == "DEBUG"

    def test_serialization_roundtrip(self) -> None:
        cfg = MachinaConfig(
            name="Test",
            connectors={"c1": ConnectorConfig(type="test")},
        )
        data = cfg.model_dump()
        restored = MachinaConfig.model_validate(data)
        assert restored.name == cfg.name
        assert restored.connectors["c1"].type == "test"

    def test_allows_extra_fields(self) -> None:
        cfg = MachinaConfig(custom_section={"key": "value"})
        assert cfg.custom_section == {"key": "value"}  # type: ignore[attr-defined]

    def test_plant_config_defaults(self) -> None:
        cfg = MachinaConfig()
        assert cfg.plant.name == "Default Plant"
        assert cfg.plant.location == ""

    def test_plant_config_custom(self) -> None:
        cfg = MachinaConfig(plant=PlantConfig(name="North Plant", location="Building A"))
        assert cfg.plant.name == "North Plant"
        assert cfg.plant.location == "Building A"

    def test_channels_default_empty(self) -> None:
        cfg = MachinaConfig()
        assert cfg.channels == []

    def test_channels_configured(self) -> None:
        cfg = MachinaConfig(
            channels=[
                ChannelConfig(type="cli"),
                ChannelConfig(type="telegram", settings={"bot_token": "tok"}),
            ]
        )
        assert len(cfg.channels) == 2
        assert cfg.channels[0].type == "cli"
        assert cfg.channels[1].settings["bot_token"] == "tok"

    def test_sandbox_default_false(self) -> None:
        cfg = MachinaConfig()
        assert cfg.sandbox is False

    def test_sandbox_enabled(self) -> None:
        cfg = MachinaConfig(sandbox=True)
        assert cfg.sandbox is True

    def test_description_field(self) -> None:
        cfg = MachinaConfig(description="Custom agent")
        assert cfg.description == "Custom agent"

    def test_full_yaml_style_config(self) -> None:
        """Validate a config that mirrors a typical machina.yaml."""
        cfg = MachinaConfig.model_validate(
            {
                "name": "Test Agent",
                "description": "Test",
                "plant": {"name": "Plant 1", "location": "Zone A"},
                "connectors": {
                    "cmms": {"type": "generic_cmms", "settings": {"data_dir": "/data"}},
                    "docs": {"type": "document_store", "settings": {"paths": ["/manuals"]}},
                },
                "channels": [{"type": "cli"}],
                "llm": {"provider": "ollama:llama3", "temperature": 0.2},
                "sandbox": True,
            }
        )
        assert cfg.name == "Test Agent"
        assert cfg.plant.name == "Plant 1"
        assert len(cfg.connectors) == 2
        assert cfg.channels[0].type == "cli"
        assert cfg.llm.provider == "ollama:llama3"
        assert cfg.sandbox is True


class TestPlantConfig:
    """Test PlantConfig validation."""

    def test_defaults(self) -> None:
        cfg = PlantConfig()
        assert cfg.name == "Default Plant"
        assert cfg.location == ""

    def test_custom(self) -> None:
        cfg = PlantConfig(name="South Plant", location="Building B")
        assert cfg.name == "South Plant"


class TestChannelConfig:
    """Test ChannelConfig validation."""

    def test_minimal(self) -> None:
        cfg = ChannelConfig(type="cli")
        assert cfg.type == "cli"
        assert cfg.settings == {}

    def test_with_settings(self) -> None:
        cfg = ChannelConfig(type="telegram", settings={"bot_token": "tok"})
        assert cfg.settings["bot_token"] == "tok"

    def test_type_required(self) -> None:
        with pytest.raises(ValidationError):
            ChannelConfig()  # type: ignore[call-arg]
