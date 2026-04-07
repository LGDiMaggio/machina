"""Tests for the configuration schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from machina.config.schema import ConnectorConfig, LLMConfig, MachinaConfig


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
        assert cfg.provider == "openai:gpt-4o"
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
