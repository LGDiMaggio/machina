"""Agent layer — runtime, prompt engineering, and entity resolution."""

from machina.agent.entity_resolver import EntityResolver, ResolvedEntity
from machina.agent.runtime import Agent
from machina.domain.citation import AgentResponse, Citation

__all__ = [
    "Agent",
    "AgentResponse",
    "Citation",
    "EntityResolver",
    "ResolvedEntity",
]
