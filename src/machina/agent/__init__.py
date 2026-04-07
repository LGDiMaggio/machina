"""Agent layer — runtime, prompt engineering, and entity resolution."""

from machina.agent.entity_resolver import EntityResolver, ResolvedEntity
from machina.agent.runtime import Agent

__all__ = ["Agent", "EntityResolver", "ResolvedEntity"]

