# Custom Connectors

Machina's connector layer is designed to be **trivial to extend**. Building a
connector for your own CMMS, IoT protocol, or messaging platform doesn't
require inheriting from an abstract base class, registering a decorator, or
filling in a plugin manifest. You write a Python class that satisfies a
duck-typed `BaseConnector` Protocol, and the agent runtime discovers what it
can do at runtime.

This page explains the `BaseConnector` contract, shows a minimal custom
connector, and links to the full API reference.

## The `BaseConnector` Protocol

Every connector must provide four things:

| Attribute | Signature | Purpose |
|---|---|---|
| `capabilities` | `ClassVar[list[str]]` | Declares which operations this connector supports (e.g. `["read_assets", "read_work_orders"]`). The agent uses this to discover capabilities at runtime and enable matching LLM tools. |
| `connect()` | `async def connect() -> None` | Establish the underlying connection (open HTTP client, log into CMMS, subscribe to broker, …). |
| `disconnect()` | `async def disconnect() -> None` | Clean up the connection. Called by `Agent.stop()`. |
| `health_check()` | `async def health_check() -> ConnectorHealth` | Return a `ConnectorHealth` status the agent can use to decide whether the connector is usable. |

Beyond those four, you implement whatever capability methods you declared.
For example, a connector with `capabilities = ["read_assets"]` must also
provide `async def read_assets(self, **kwargs) -> list[Asset]`.

## Minimal example

Here's a complete custom connector that reads assets from an imaginary
"Acme CMMS" REST API:

```python
from typing import Any, ClassVar

import httpx

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.domain.asset import Asset, AssetType, Criticality


class AcmeCmmsConnector:
    """Custom connector for the Acme CMMS REST API."""

    capabilities: ClassVar[list[str]] = ["read_assets"]

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._connected = False

    async def connect(self) -> None:
        # Real implementations typically verify reachability here
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def health_check(self) -> ConnectorHealth:
        status = ConnectorStatus.HEALTHY if self._connected else ConnectorStatus.UNHEALTHY
        return ConnectorHealth(status=status, message="")

    async def read_assets(self, **kwargs: Any) -> list[Asset]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/equipment",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
        return [
            Asset(
                id=str(item["tag"]),
                name=item["description"],
                type=AssetType.ROTATING_EQUIPMENT,
                criticality=Criticality(item.get("criticality", "C")),
                equipment_class_code=item.get("iso_code"),
            )
            for item in resp.json()
        ]
```

Register it with an agent the same way as any built-in connector:

```python
from machina import Agent, Plant

agent = Agent(
    plant=Plant(name="Acme Plant 1"),
    connectors=[AcmeCmmsConnector(base_url="https://cmms.acme.com", api_key="…")],
    llm="openai:gpt-4o",
)
agent.run()
```

## Design rules (CLAUDE.md conventions)

- **Always return domain entities.** Never return raw API payloads to the
  agent runtime — normalize everything into `Asset`, `WorkOrder`, `FailureMode`,
  etc. This keeps the agent layer connector-agnostic.
- **Everything async.** Use `async def` for any I/O. The agent runtime uses
  `asyncio.gather` to call multiple connectors in parallel — a sync call would
  block the event loop.
- **Structured logging.** Use `structlog` and include `connector=`, `asset_id=`,
  and `operation=` in every log line so operators can trace issues.
- **Graceful degradation.** Declare only the capabilities you actually support.
  If your CMMS doesn't expose spare parts, omit `"read_spare_parts"` — the
  agent will simply not offer that tool to the LLM.

## API Reference

::: machina.connectors.base.BaseConnector

::: machina.connectors.base.ConnectorHealth

::: machina.connectors.base.ConnectorStatus

::: machina.connectors.base.ConnectorRegistry
