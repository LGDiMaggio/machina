"""BaseConnector protocol and ConnectorRegistry.

Connectors declare their capabilities so the agent can discover at
runtime what actions are available.  This enables graceful degradation —
the agent works with whatever connectors are configured.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ConnectorStatus(StrEnum):
    """Health status of a connector."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ConnectorHealth(BaseModel):
    """Result of a connector health check."""

    status: ConnectorStatus
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class BaseConnector(Protocol):
    """Protocol that all Machina connectors must satisfy.

    Connectors are the integration layer between Machina and external
    systems (CMMS, IoT, ERP, communication platforms, document stores).
    """

    @property
    def capabilities(self) -> list[str]:
        """Declarative list of actions this connector supports.

        Examples: ``["read_assets", "read_work_orders", "create_work_order"]``
        """
        ...

    async def connect(self) -> None:
        """Establish a connection to the external system."""
        ...

    async def disconnect(self) -> None:
        """Gracefully close the connection."""
        ...

    async def health_check(self) -> ConnectorHealth:
        """Check whether the external system is reachable and responsive."""
        ...


class ConnectorRegistry:
    """Registry for discovering connectors by capability.

    Connectors register themselves, and the agent (or MCP layer) can
    query which connectors support a given capability.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}

    def register(self, name: str, connector: BaseConnector) -> None:
        """Register a connector under the given name."""
        self._connectors[name] = connector

    def get(self, name: str) -> BaseConnector | None:
        """Retrieve a connector by name."""
        return self._connectors.get(name)

    def find_by_capability(self, capability: str) -> list[tuple[str, BaseConnector]]:
        """Return all connectors that declare the given capability."""
        return [
            (name, conn)
            for name, conn in self._connectors.items()
            if capability in conn.capabilities
        ]

    def all(self) -> dict[str, BaseConnector]:
        """Return all registered connectors."""
        return dict(self._connectors)
