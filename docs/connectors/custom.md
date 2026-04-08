# Custom Connectors

Machina's connector layer is designed to be **trivial to extend**. Most
CMMS integrations can be expressed as configuration of the built-in
`GenericCmmsConnector`; only exotic protocols (SOAP, OData, WebSockets)
or custom business logic call for writing a new class from scratch.

This page walks through both paths:

- **Path A — Configure `GenericCmmsConnector`** for any REST-based CMMS,
  picking an authentication strategy, a pagination strategy, and an
  optional JMESPath field mapping.
- **Path B — Write a custom connector** by satisfying the `BaseConnector`
  Protocol.

Both paths end at the same place: a connector object you can hand to an
`Agent`.

## Which path do I pick?

| Your CMMS… | Use |
|---|---|
| Has a REST + JSON API, any auth + pagination style | **Path A** (configure `GenericCmmsConnector`) |
| Returns deeply nested response payloads | **Path A** with `_fields` JMESPath mapping |
| Uses OData (SAP PM), SOAP, gRPC, or WebSockets | **Path B** (custom class) |
| Requires custom business logic (rate limiting, caching, batch writes) | **Path B** |
| Needs to integrate two systems behind one connector | **Path B** |

Start with Path A. Drop down to Path B only when configuration can't
express what you need.

## Path A — Configure `GenericCmmsConnector`

### The five capabilities

`GenericCmmsConnector` declares five CMMS capabilities that the agent
runtime discovers at startup:

| Capability | What it reads/writes |
|---|---|
| `read_assets` | Returns all known assets (paginated if configured) |
| `read_work_orders` | Returns work orders, optionally filtered by `asset_id` / `status` |
| `create_work_order` | POSTs a new work order to the CMMS |
| `read_spare_parts` | Returns spare parts, optionally filtered by `asset_id` / `sku` |
| `read_maintenance_history` | Returns completed work orders for a given asset |

### Authentication strategies

Pick the one that matches your CMMS:

```python
from machina.connectors.cmms import (
    BearerAuth,
    BasicAuth,
    ApiKeyHeaderAuth,
    NoAuth,
)

# Bearer token (default for most modern CMMS — UpKeep, MaintainX, Limble)
auth = BearerAuth(token="eyJhbGci...")

# HTTP Basic auth (older/on-prem deployments)
auth = BasicAuth(username="svc", password="...")

# API key in a custom header (e.g. X-API-Key)
auth = ApiKeyHeaderAuth(header_name="X-API-Key", value="k-123")

# No auth — public or intranet-only endpoints
auth = NoAuth()
```

For backwards compatibility, passing `api_key="..."` to the constructor
is still accepted and is equivalent to `auth=BearerAuth(token=api_key)`.

### Pagination strategies

Pick the one that matches your CMMS's list endpoints:

```python
from machina.connectors.cmms import (
    NoPagination,
    OffsetLimitPagination,
    PageNumberPagination,
    CursorPagination,
)

# Single-shot GET — response is the full list (default)
pagination = NoPagination()

# ?offset=X&limit=Y style (UpKeep, Limble, and most REST CMMS)
pagination = OffsetLimitPagination(
    limit_param="limit",
    offset_param="offset",
    page_size=100,
)

# ?page=N&per_page=M style (GitHub-style APIs)
pagination = PageNumberPagination(
    page_param="page",
    size_param="per_page",
    page_size=50,
    start_page=1,  # use 0 if your API is zero-indexed
)

# Opaque cursor token from the response
pagination = CursorPagination(
    cursor_param="cursor",
    cursor_response_path="next_cursor",  # JMESPath
    items_path="items",                  # JMESPath
)
```

All strategies accept an optional `items_path` (JMESPath) for extracting
the list from a wrapped response like `{"data": [...], "meta": {...}}`.
`CursorPagination` requires it because it always walks a wrapped object.

### Schema mapping

The `schema_mapping` parameter lets you bridge differences between what
the CMMS returns and what Machina expects, in two flavours:

**Flat rename** — top-level key rewrites:

```python
cmms = GenericCmmsConnector(
    url="https://cmms.example.com/api",
    auth=BearerAuth(token="..."),
    schema_mapping={
        "assets": {"asset_id": "id", "display_name": "name"},
    },
)
```

**JMESPath extraction** — for deeply nested response items:

```python
cmms = GenericCmmsConnector(
    url="https://cmms.example.com/api",
    auth=BearerAuth(token="..."),
    schema_mapping={
        "assets": {
            "_fields": {
                "id": "equipment.id",
                "name": "equipment.display_name",
                "criticality": "meta.criticality_class",
                "equipment_class_code": "meta.iso_code",
            },
        },
    },
)
```

The presence of the `_fields` sentinel switches the mapper into
JMESPath mode. Each mapping value is a JMESPath expression evaluated
against the raw item; missing paths are silently dropped.

Supported entity keys in `schema_mapping`: `assets`, `work_orders`,
`spare_parts`.

### Putting it all together

A "modern CMMS" example combining Basic auth, offset/limit pagination
with custom param names, and nested-response field extraction:

```python
from machina.connectors.cmms import (
    BasicAuth,
    GenericCmmsConnector,
    OffsetLimitPagination,
)

cmms = GenericCmmsConnector(
    url="https://modern-cmms.example.com/v2",
    auth=BasicAuth(username="svc", password="s3cret"),
    pagination=OffsetLimitPagination(
        limit_param="size",
        offset_param="start",
        page_size=50,
        items_path="data",
    ),
    schema_mapping={
        "assets": {
            "_fields": {
                "id": "equipment.id",
                "name": "equipment.display_name",
                "criticality": "meta.criticality_class",
                "equipment_class_code": "meta.iso_code",
            },
        },
    },
)
```

For offline / demo scenarios, point `data_dir` at a directory of
`assets.json`, `work_orders.json`, and `spare_parts.json` files — the
connector will load them into memory without touching the network:

```python
cmms = GenericCmmsConnector(data_dir="sample_data/cmms")
```

## Path B — Write a custom connector

For protocols and patterns `GenericCmmsConnector` can't express, write a
Python class that satisfies the `BaseConnector` Protocol.

### The `BaseConnector` Protocol

Every connector must provide four things:

| Attribute | Signature | Purpose |
|---|---|---|
| `capabilities` | `ClassVar[list[str]]` | Declares which operations this connector supports (e.g. `["read_assets", "read_work_orders"]`). The agent uses this to discover capabilities at runtime and enable matching LLM tools. |
| `connect()` | `async def connect() -> None` | Establish the underlying connection (open HTTP client, log into CMMS, subscribe to broker, …). |
| `disconnect()` | `async def disconnect() -> None` | Clean up the connection. Called by `Agent.stop()`. |
| `health_check()` | `async def health_check() -> ConnectorHealth` | Return a `ConnectorHealth` status the agent can use to decide whether the connector is usable. |

Beyond those four, implement whatever capability methods you declared.
A connector with `capabilities = ["read_assets"]` must also provide
`async def read_assets(self, **kwargs) -> list[Asset]`.

### Minimal example

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

## Testing conventions

Machina connectors follow a two-layer test strategy — unit tests for
logic, integration tests for the HTTP surface. Use the built-in CMMS
tests as canonical examples:

- `tests/unit/test_generic_cmms.py` — exercises `GenericCmmsConnector`
  in local mode, plus pure-logic tests for auth strategies, pagination
  strategies, and schema mapping. Pagination is tested against a small
  fake client (`_FakeClient` / `_FakeResponse`) rather than real httpx,
  so the tests stay fast and don't require network mocks.
- `tests/integration/test_generic_cmms_rest.py` — exercises the full
  REST path via [`pytest-httpx`](https://pypi.org/project/pytest-httpx/)
  mock fixtures. Tests combine auth, pagination, and mapping end-to-end
  against a simulated CMMS API.

A minimal REST test looks like this:

```python
import pytest

from machina.connectors.cmms import BearerAuth, GenericCmmsConnector

BASE_URL = "https://cmms.example.com/api"


@pytest.mark.asyncio
async def test_reads_assets(httpx_mock) -> None:
    conn = GenericCmmsConnector(url=BASE_URL, auth=BearerAuth(token="test"))
    httpx_mock.add_response(
        method="GET", url=f"{BASE_URL}/health", status_code=200, json={"status": "ok"}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE_URL}/assets",
        json=[{"id": "P-201", "name": "Pump", "type": "rotating_equipment"}],
    )
    await conn.connect()
    assets = await conn.read_assets()
    assert len(assets) == 1
    assert assets[0].id == "P-201"
```

Never hit real external APIs from tests — always use `pytest-httpx` for
REST mocking or VCR for recorded responses.

## Design rules (CLAUDE.md conventions)

- **Always return domain entities.** Never return raw API payloads to the
  agent runtime — normalize everything into `Asset`, `WorkOrder`,
  `FailureMode`, etc. This keeps the agent layer connector-agnostic.
- **Everything async.** Use `async def` for any I/O. The agent runtime
  uses `asyncio.gather` to call multiple connectors in parallel — a sync
  call would block the event loop.
- **Structured logging.** Use `structlog` and include `connector=`,
  `asset_id=`, and `operation=` in every log line so operators can trace
  issues.
- **Graceful degradation.** Declare only the capabilities you actually
  support. If your CMMS doesn't expose spare parts, omit
  `"read_spare_parts"` — the agent will simply not offer that tool to
  the LLM.

## API Reference

::: machina.connectors.base.BaseConnector

::: machina.connectors.base.ConnectorHealth

::: machina.connectors.base.ConnectorStatus

::: machina.connectors.base.ConnectorRegistry

::: machina.connectors.cmms.generic.GenericCmmsConnector

::: machina.connectors.cmms.auth.BearerAuth

::: machina.connectors.cmms.auth.BasicAuth

::: machina.connectors.cmms.auth.ApiKeyHeaderAuth

::: machina.connectors.cmms.auth.NoAuth

::: machina.connectors.cmms.pagination.NoPagination

::: machina.connectors.cmms.pagination.OffsetLimitPagination

::: machina.connectors.cmms.pagination.PageNumberPagination

::: machina.connectors.cmms.pagination.CursorPagination
