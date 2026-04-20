"""Pydantic models for the GenericCmms YAML mapper schema.

Defines the declarative contract between a YAML config file and the
GenericCmmsConnector's YAML-driven mapping engine.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from machina.connectors.cmms.generic_coercers import COERCER_REGISTRY


class FieldSpec(BaseModel):
    """Mapping spec for a single entity field."""

    source: str = Field(..., description="JSONPath-lite path into the raw API response")
    coerce: str | None = Field(default=None, description="Named coercer to apply")
    enum_map: dict[str, str] | None = Field(default=None, description="Value lookup table")
    default: Any = Field(default=None, description="Default when source is missing/null")
    required: bool = Field(default=False, description="Skip row if this field is missing")
    pattern: str | None = Field(
        default=None, description="Regex pattern for regex_extract coercer"
    )

    @model_validator(mode="after")
    def _validate_coercer(self) -> FieldSpec:
        if self.coerce and self.coerce != "enum_map" and self.coerce not in COERCER_REGISTRY:
            msg = f"Unknown coercer: {self.coerce!r}. Available: {sorted(COERCER_REGISTRY.keys())}"
            raise ValueError(msg)
        if self.coerce == "regex_extract" and not self.pattern:
            msg = "regex_extract coercer requires a 'pattern' field"
            raise ValueError(msg)
        if self.coerce == "enum_map" and not self.enum_map:
            msg = "enum_map coercer requires an 'enum_map' field"
            raise ValueError(msg)
        return self


class ReverseFieldSpec(BaseModel):
    """Reverse mapping spec: domain field → external API field for writes."""

    target: str = Field(..., description="Target field name in the outbound JSON")
    reverse_enum_map: dict[str, str] | None = Field(
        default=None, description="Domain value → external value lookup"
    )


class EndpointSpec(BaseModel):
    """HTTP endpoint configuration for a mapped entity."""

    method: str = Field(default="GET")
    path: str = Field(..., description="API path (appended to base_url)")
    pagination: dict[str, Any] | None = Field(default=None)


class EntityMapping(BaseModel):
    """Complete mapping for one entity type (Asset or WorkOrder)."""

    endpoint: EndpointSpec
    root: str = Field(default="", description="JSONPath-lite root for the items array")
    fields: dict[str, FieldSpec | dict[str, FieldSpec]] = Field(
        ..., min_length=1, description="entity_field → FieldSpec, or metadata → {key → FieldSpec}"
    )
    create_endpoint: EndpointSpec | None = Field(default=None)
    reverse_fields: dict[str, str | ReverseFieldSpec] | None = Field(
        default=None, description="Domain field → external field for create/update"
    )


class GenericCmmsYamlConfig(BaseModel):
    """Top-level YAML mapper configuration for GenericCmmsConnector."""

    mapping: dict[str, EntityMapping] = Field(
        ..., min_length=1, description="Entity type mappings (e.g. 'asset', 'work_order')"
    )

    @model_validator(mode="after")
    def _validate_entity_types(self) -> GenericCmmsYamlConfig:
        valid = {"asset", "work_order"}
        for key in self.mapping:
            if key not in valid:
                msg = f"Unknown entity type: {key!r}. Valid types: {sorted(valid)}"
                raise ValueError(msg)
        return self
