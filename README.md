<div align="center">
  <img src="docs/assets/machina-logo.svg" alt="Machina" width="700"/>
  <h1>Machina</h1>
  <p><strong>The open-source Python framework for building AI agents specialized in industrial maintenance.</strong></p>

  [![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
  [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
  [![PyPI version](https://img.shields.io/pypi/v/machina-ai.svg)](https://pypi.org/project/machina-ai/)
  [![CI](https://img.shields.io/github/actions/workflow/status/LGDiMaggio/machina/ci.yml?branch=main)](https://github.com/LGDiMaggio/machina/actions)
  [![Downloads](https://img.shields.io/pypi/dm/machina-ai.svg)](https://pypi.org/project/machina-ai/)

  <p>
    <a href="#quick-start">Quick Start</a> •
    <a href="#features">Features</a> •
    <a href="#architecture">Architecture</a> •
    <a href="#connectors">Connectors</a> •
    <a href="#mcp-server">MCP Server</a> •
    <a href="https://machina-ai.readthedocs.io">Documentation</a> •
    <a href="#contributing">Contributing</a>
  </p>
</div>

---

> **Machina** is to industrial maintenance what LangChain is to general-purpose LLM applications.
> Build AI agents that talk to your CMMS, read your equipment manuals, monitor your sensors, and help your technicians — all in a few lines of Python.

## Why Machina?

Building an AI maintenance agent today means writing custom connectors for SAP PM, IBM Maximo, or whatever CMMS your plant uses. It means defining domain concepts like assets, work orders, and failure modes from scratch. It means handling OPC-UA subscriptions, Modbus registers, and MQTT topics. And it means engineering prompts that understand maintenance — all before writing a single line of business logic.

**That takes months. Machina makes it take minutes.**

Machina provides the missing vertical layer between general-purpose agent frameworks (LangChain, CrewAI, AutoGen) and the industrial maintenance world: pre-built connectors, a rich domain model aligned with ISO 14224, and maintenance-aware AI — so you can go from `pip install` to a working agent in under 30 minutes.

## Features

- **Industrial Connectors** — Pre-built integrations for CMMS (SAP PM, IBM Maximo, UpKeep, any REST-based CMMS), document stores with RAG, and communication platforms (Telegram). IoT protocols (OPC-UA, MQTT) and additional comms (Slack, Teams) coming in v0.2
- **Maintenance Domain Model** — First-class Python objects for Asset, WorkOrder, FailureMode, SparePart, MaintenancePlan, and Alarm — with hierarchies, validation, and domain logic built in
- **Domain-Aware AI** — Agents that automatically resolve equipment references, inject maintenance context, retrieve relevant procedures, and ground answers in your data
- **LLM-Agnostic** — Works with OpenAI, Anthropic, Mistral, Llama, Ollama, and any LiteLLM-compatible provider. No vendor lock-in
- **Async-First** — Built on `asyncio` for concurrent queries and high-throughput production environments
- **Pluggable Auth & Pagination** — Built-in support for OAuth2, API key, Basic Auth, and Bearer token authentication; offset, page-number, and cursor pagination strategies; exponential backoff retry logic
- **MCP Server** *(v0.2)* — Expose any connector as an MCP server — let Claude Desktop, Cursor, or any MCP client query your CMMS and sensors without writing agent code
- **Workflow Engine** — Composable multi-step workflows with trigger-step-action model, template variable interpolation, error policies (retry/skip/stop/notify), guard conditions, and sandbox mode. Includes built-in alarm-to-work-order template
- **Sandbox Mode** — Test agents safely with a log-only runtime that records all actions without executing them — perfect for demos and experimentation
- **Extensible** — Create custom connectors, domain entities, and workflows. Publish them as plugins for the community

## Quick Start

### Installation

```bash
pip install machina-ai
```

Install with specific connectors:

```bash
pip install machina-ai[sap]       # SAP PM connector
pip install machina-ai[opcua]     # OPC-UA connector
pip install machina-ai[telegram]  # Telegram connector
pip install machina-ai[all]       # Everything
```

### Your First Maintenance Agent in 10 Lines

```python
from machina import Agent, Plant
from machina.connectors import DocumentStore, Telegram

# Load equipment manuals for RAG
docs = DocumentStore(paths=["./manuals/", "./procedures/"])

# Connect to Telegram for technician interaction
telegram = Telegram(bot_token="YOUR_BOT_TOKEN")

# Create the agent
agent = Agent(
    name="Maintenance Assistant",
    connectors=[docs],
    channels=[telegram],
    llm="openai:gpt-4o",  # or "ollama:llama3", "anthropic:claude-sonnet"
)

agent.run()
```

That's it. Your technicians can now ask questions on Telegram like:

> *"What's the procedure for replacing the bearing on pump P-201?"*
> *"How many times has the compressor C-102 failed in the last year?"*
> *"Are there spare parts available for the heat exchanger HX-05?"*

The agent retrieves answers from your manuals, CMMS history, and spare part inventory — and responds in the technician's language.

### Connect Your CMMS

```python
from machina.connectors import SapPM

cmms = SapPM(
    url="https://sap.yourcompany.com/odata/v4",
    client_id="...",
    client_secret="...",
)

# The agent now has access to work orders, asset history, maintenance plans
agent = Agent(
    name="Maintenance Assistant",
    connectors=[docs, cmms],
    channels=[telegram],
    llm="openai:gpt-4o",
)
```

### Add Real-Time Sensor Monitoring *(Coming in v0.2)*

```python
from machina.connectors import OpcUa  # requires: pip install machina-ai[opcua]

sensors = OpcUa(
    endpoint="opc.tcp://plc-line2:4840",
    subscriptions=[
        {"node_id": "ns=2;s=Pump.P201.Vibration", "interval_ms": 1000},
        {"node_id": "ns=2;s=Pump.P201.Temperature", "interval_ms": 5000},
    ],
)

agent = Agent(
    name="Predictive Maintenance Agent",
    connectors=[docs, cmms, sensors],
    channels=[telegram],
    llm="openai:gpt-4o",
)
```

> **Note:** The OPC-UA connector is planned for v0.2. The example above shows the target API.

## Architecture

Machina follows a layered architecture with clear separation of concerns:

```
                    ┌───────────────────────────┐
                    │   Claude / Cursor / MCP   │
                    └─────────────┬─────────────┘
                                  │ MCP Protocol
┌─────────────────────────────────┼─────────────────────┐
│              YOUR APPLICATION   │                      │
│  ┌─────────────────────┐  ┌────┴──────────────────┐  │
│  │    AGENT LAYER       │  │    MCP SERVER LAYER   │  │
│  │ Runtime · Workflows  │  │  (auto-generated from │  │
│  │ Domain Prompting     │  │   connector caps)     │  │
│  └──────────┬──────────┘  └────────┬──────────────┘  │
├─────────────┴──────────────────────┴──────────────────┤
│                    DOMAIN LAYER                        │
│  Asset · WorkOrder · FailureMode · SparePart · Alarm  │
├───────────────────────────────────────────────────────┤
│                  CONNECTOR LAYER                       │
│  CMMS · IoT · ERP · Communication · Documents         │
├───────────────────────────────────────────────────────┤
│                    CORE LAYER                          │
│      LLM Abstraction · Config · Observability         │
└───────────────────────────────────────────────────────┘
```

**Design principles:** modular and pluggable (install only what you need), convention over configuration (sensible defaults), domain-first (every connector normalizes to domain entities), LLM-agnostic, and observable (structured logging and tracing for every action).

See the [Architecture Guide](https://machina-ai.readthedocs.io/en/latest/architecture/) for details.

## Connectors

### CMMS

#### ✅ Available Now

| Connector | System | Since |
|-----------|--------|-------|
| `GenericCmms` | Any REST-based CMMS (configurable via YAML/JSON schema mapping) | v0.1 |
| `SapPM` | SAP Plant Maintenance (OData v2/v4, OAuth2 + Basic Auth) | v0.1 |
| `Maximo` | IBM Maximo (OSLC/JSON API, API key + Basic + Bearer Auth) | v0.1 |
| `UpKeep` | UpKeep CMMS (REST API v2, Session-Token Auth) | v0.1 |

All CMMS connectors include pluggable authentication (OAuth2, API key, Basic, Bearer), pagination strategies (offset, page-number, cursor), and exponential backoff retry logic.

#### 🚧 Coming Soon

| Connector | System | Planned |
|-----------|--------|--------|
| `MaintainX` | MaintainX | v0.2 |
| `Limble` | Limble CMMS | v0.2 |
| `Fiix` | Fiix (Rockwell) | v0.2 |
| `eMaint` | eMaint (Fluke) | v0.3 |
| `InforEam` | Infor EAM | v0.3 |

### IoT & Industrial Protocols

#### 🚧 Coming Soon

| Connector | Protocol | Planned |
|-----------|----------|---------|
| `OpcUa` | OPC-UA | v0.2 |
| `Mqtt` | MQTT / Sparkplug B | v0.2 |
| `Modbus` | Modbus TCP/RTU | v0.3 |
| `Plc` | S7 / EtherNet/IP | v0.3 |

### Communication

#### ✅ Available Now

| Connector | Platform | Since |
|-----------|----------|-------|
| `Telegram` | Telegram Bot API | v0.1 |

#### 🚧 Coming Soon

| Connector | Platform | Planned |
|-----------|----------|---------|
| `WhatsApp` | WhatsApp Business | v0.2 |
| `Slack` | Slack Bot API | v0.2 |
| `Teams` | Microsoft Teams | v0.2 |
| `Email` | SMTP / IMAP / Gmail API | v0.2 |
| `GoogleChat` | Google Chat | v0.3 |

### Documents & Knowledge

#### ✅ Available Now

| Connector | Source | Since |
|-----------|--------|-------|
| `DocumentStore` | PDF / DOCX with RAG | v0.1 |

#### 🚧 Coming Soon

| Connector | Source | Planned |
|-----------|--------|---------|
| `Confluence` | Atlassian | v0.3 |
| `SharePoint` | Microsoft 365 | v0.3 |

### ERP

#### 🚧 Coming Soon

| Connector | System | Planned |
|-----------|--------|---------|
| `SapErp` | SAP S/4HANA | v0.2 |
| `OracleErp` | Oracle ERP | v0.3 |

### Building Custom Connectors

```python
from machina.connectors import BaseConnector

class MyCustomCmms(BaseConnector):
    capabilities = ["read_assets", "read_work_orders", "create_work_order"]

    async def connect(self):
        # Your connection logic
        ...

    async def read_assets(self) -> list[Asset]:
        # Your implementation
        ...
```

See the [Custom Connectors Guide](https://machina-ai.readthedocs.io/en/latest/connectors/custom/) for the full tutorial.

## Domain Model

Machina ships with a rich domain model that encodes industrial maintenance concepts:

```python
from machina.domain import Asset, AssetType, FailureMode

# Define your equipment
pump = Asset(
    id="P-201",
    name="Cooling Water Pump",
    type=AssetType.ROTATING_EQUIPMENT,
    location="Building A / Line 2 / Cooling System",
    criticality="A",
    equipment_class_code="PU",  # ISO 14224 Table A.4
)

# Define known failure modes
bearing_wear = FailureMode(
    code="BEAR-WEAR-01",               # Machina-internal catalog ID
    iso_14224_code="VIB",              # ISO 14224 Annex B Table B.15
    name="Bearing Wear — Drive End",
    mechanism="fatigue",               # ISO 14224 Table B.2
    category="mechanical",
    detection_methods=["vibration_analysis", "temperature_monitoring"],
    recommended_actions=["replace_bearing", "check_alignment"],
)
```

The domain model supports hierarchical asset trees, ISO 14224-aligned failure taxonomies, work order lifecycle management, spare part inventory tracking, and maintenance plan scheduling.

See the [Domain Model Reference](https://machina-ai.readthedocs.io/en/latest/domain/) for all entities and services.

## Workflow Engine

Build multi-step maintenance workflows with error handling, template variable interpolation, and sandbox mode:

```python
from machina.workflows import Workflow, Step, Trigger, TriggerType, ErrorPolicy

alarm_to_workorder = Workflow(
    name="Alarm to Work Order",
    trigger=Trigger(type=TriggerType.ALARM, filter={"severity": ["critical"]}),
    steps=[
        Step("diagnose", action="failure_analyzer.diagnose",
             on_error=ErrorPolicy.STOP),
        Step("check_history", action="cmms.get_asset_history",
             inputs={"asset_id": "{trigger.asset_id}"},
             on_error=ErrorPolicy.SKIP),
        Step("create_wo", action="work_order_factory.create",
             on_error=ErrorPolicy.STOP),
        Step("notify", action="channels.send_message",
             template="⚠️ WO created for {trigger.asset_id}: {diagnose}",
             on_error=ErrorPolicy.NOTIFY),
    ],
)

agent = Agent(workflows=[alarm_to_workorder], sandbox=True)
agent.register_workflow(alarm_to_workorder)
result = await agent.trigger_workflow("Alarm to Work Order", {"asset_id": "P-201"})
```

Or use the built-in alarm-to-work-order template:

```python
from machina.workflows.builtins import alarm_to_workorder
agent.register_workflow(alarm_to_workorder)
```

Workflow features:
- **Trigger types**: alarm, schedule, manual, condition
- **Error policies**: retry (with configurable retries), skip, stop, notify
- **Guard conditions**: skip steps based on prior outputs
- **Template variables**: `{trigger.asset_id}`, `{step_name}`, `{step_name.field}`
- **Sandbox mode**: write actions logged but not executed — reads still run
- **Observability**: every step traced via ActionTracer

## MCP Server *(Coming in v0.2)*

Don't need a full agent? Machina will expose its connectors as **MCP (Model Context Protocol) servers**, so Claude Desktop, Cursor, or any MCP-compatible tool can access your industrial data directly:

```bash
# Start MCP server exposing your CMMS and document store
machina mcp serve --config machina.yaml
```

Anyone on your team will be able to ask Claude: *"What's the maintenance history for pump P-201?"* — and Claude queries your SAP PM instance through Machina's MCP server. No agent code required.

This will also be the fastest way to evaluate Machina: connect your data, use it from Claude, and when you need workflows and automation, the full framework is right there.

> **Note:** The MCP server layer is planned for v0.2. The connector infrastructure is already in place — the MCP layer will be a thin protocol adapter on top.

## Roadmap

### ✅ v0.1 — Maintenance Knowledge Agent *(released)*

- [x] Core domain model (Asset, WorkOrder, FailureMode, SparePart, Alarm, MaintenancePlan)
- [x] Domain services (FailureAnalyzer, WorkOrderFactory, MaintenanceScheduler)
- [x] BaseConnector protocol and ConnectorRegistry
- [x] Exception hierarchy and structured logging (structlog)
- [x] Configuration system (YAML + env var substitution + validation)
- [x] LLM abstraction layer (LiteLLM wrapper with function calling)
- [x] GenericCmmsConnector (any REST-based CMMS)
- [x] SapPmConnector (OData v2/v4, OAuth2 + Basic Auth)
- [x] MaximoConnector (OSLC/JSON API, API key + Basic + Bearer Auth)
- [x] UpKeepConnector (REST API v2, Session-Token Auth)
- [x] Pluggable auth (OAuth2, API key, Basic, Bearer), pagination (offset, page, cursor), and retry logic
- [x] DocumentStore connector with RAG (LangChain + ChromaDB)
- [x] Telegram connector
- [x] Agent runtime with domain-aware prompting
- [x] Entity resolver (natural language → asset resolution)
- [x] Action tracing (observability)
- [x] LLM tool definitions (function calling)
- [x] CI/CD pipeline (GitHub Actions) with automated PyPI release

### 🚧 v0.2 — Workflows, MCP & More Connectors *(in progress)*

- [ ] MaintainX, Limble, Fiix connectors
- [ ] OPC-UA and MQTT connectors
- [ ] WhatsApp, Slack, Teams, Email connectors
- [x] Workflow engine with trigger-step-action model
- [x] Built-in alarm-to-work-order workflow template
- [x] Sandbox mode — log-only runtime
- [ ] **MCP Server layer** — use connectors from Claude, Cursor, and any MCP client
- [ ] Plugin system for community extensions

### 🔮 v0.3 — Intelligence & Scale

- [ ] Anomaly detection module
- [ ] Multi-agent orchestration
- [ ] Remaining Useful Life (RUL) estimation
- [ ] Additional connectors (Modbus, eMaint, Infor EAM, GoogleChat)

See the [full roadmap](https://github.com/LGDiMaggio/machina/projects) for details.

## Examples

The [`examples/`](examples/) directory contains complete, runnable examples:

| Example | Description | Status |
|---------|-------------|--------|
| [`knowledge_agent/`](examples/knowledge_agent/) | Maintenance Knowledge Agent — Q&A chatbot with RAG | ✅ Available |
| [`predictive_pipeline/`](examples/predictive_pipeline/) | End-to-end predictive maintenance: sensor alarm → diagnosis → work order → scheduling | ⚠️ Preview — example code ready, uses workflow engine |
| `alarm_to_workorder/` | Alarm-to-Work-Order workflow with CMMS integration | ✅ Available (built-in template) |
| `multi_agent_team/` | Specialized agents collaborating on complex diagnostics | 🚧 Planned (v0.3) |

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

### Development Setup

```bash
# Clone and install
git clone https://github.com/LGDiMaggio/machina.git
cd machina
pip install -e ".[dev,all]"

# Run checks
make lint          # Lint (ruff)
make typecheck     # Type check (mypy strict)
make test          # Tests with coverage
make ci            # All of the above
```

## Community & Support

- [GitHub Discussions](https://github.com/LGDiMaggio/machina/discussions) — Ask questions, share ideas, show what you've built
- [Issues](https://github.com/LGDiMaggio/machina/issues) — Report bugs and request features
- [Twitter/X](https://twitter.com/machina_oss) — Updates and announcements

## License

Distributed under the Apache License 2.0. See [`LICENSE`](LICENSE) for more information.

## Acknowledgments

Machina builds on the shoulders of giants:

- [LiteLLM](https://github.com/BerriAI/litellm) — LLM provider abstraction
- [LangChain](https://github.com/langchain-ai/langchain) — Document loaders and RAG primitives
- [asyncua](https://github.com/FreeOpcUa/opcua-asyncio) — OPC-UA Python implementation
- [Paho MQTT](https://github.com/eclipse/paho.mqtt.python) — MQTT client
- [ChromaDB](https://github.com/chroma-core/chroma) — Vector database for RAG

---

<div align="center">
  <strong>Built for the people who keep the machines running.</strong>
</div>
