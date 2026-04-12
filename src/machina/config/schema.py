"""Configuration schema — pydantic models for validating machina.yaml."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConnectorConfig(BaseModel):
    """Configuration for a single connector instance."""

    type: str = Field(..., description="Connector type (e.g. 'generic_cmms', 'opcua')")
    enabled: bool = Field(default=True)
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Connector-specific settings",
    )

    model_config = {"extra": "allow"}


class LLMConfig(BaseModel):
    """Configuration for the LLM provider."""

    provider: str = Field(
        default="openai:gpt-4o",
        description="Provider and model in 'provider:model' format",
    )
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1)

    model_config = {"extra": "allow"}


class PlantConfig(BaseModel):
    """Configuration for the plant (top-level asset container)."""

    name: str = Field(default="Default Plant", description="Plant name")
    location: str = Field(default="", description="Plant location")


class ChannelConfig(BaseModel):
    """Configuration for a communication channel."""

    type: str = Field(..., description="Channel type (e.g. 'cli', 'telegram', 'slack')")
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Channel-specific settings",
    )


class MachinaConfig(BaseModel):
    """Top-level Machina configuration.

    Example YAML::

        name: "Maintenance Assistant"
        plant:
          name: "North Plant"
        connectors:
          cmms:
            type: generic_cmms
            settings:
              data_dir: "./sample_data/cmms"
        channels:
          - type: cli
        llm:
          provider: "ollama:llama3"
        sandbox: false
    """

    name: str = Field(default="Machina Agent", description="Agent name")
    description: str = Field(
        default="Maintenance AI assistant",
        description="Agent description",
    )
    plant: PlantConfig = Field(default_factory=PlantConfig)
    connectors: dict[str, ConnectorConfig] = Field(
        default_factory=dict,
        description="Named connector configurations",
    )
    channels: list[ChannelConfig] = Field(
        default_factory=list,
        description="Communication channels (defaults to CLI if empty)",
    )
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sandbox: bool = Field(
        default=False, description="Enable sandbox mode (writes are logged, not executed)"
    )
    logging: dict[str, Any] = Field(
        default_factory=dict,
        description="Logging configuration overrides",
    )

    model_config = {"extra": "allow"}
