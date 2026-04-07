"""Configuration schema — pydantic models for validating machina.yaml."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConnectorConfig(BaseModel):
    """Configuration for a single connector instance."""

    type: str = Field(..., description="Connector type (e.g. 'sap_pm', 'opcua')")
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


class MachinaConfig(BaseModel):
    """Top-level Machina configuration."""

    name: str = Field(default="Machina Agent", description="Agent name")
    connectors: dict[str, ConnectorConfig] = Field(
        default_factory=dict,
        description="Named connector configurations",
    )
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: dict[str, Any] = Field(
        default_factory=dict,
        description="Logging configuration overrides",
    )

    model_config = {"extra": "allow"}
