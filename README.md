<div align="center">
  <img src="docs/assets/machina-logo.svg" alt="Machina" width="500"/>
  <h1>Machina</h1>
  <p><strong>The open-source Python framework for building AI agents specialized in industrial maintenance.</strong></p>

  [![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
  [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
  [![PyPI version](https://img.shields.io/pypi/v/machina-ai.svg)](https://pypi.org/project/machina-ai/)
  [![CI](https://img.shields.io/github/actions/workflow/status/LGDiMaggio/machina/ci.yml?branch=main)](https://github.com/LGDiMaggio/machina/actions)
  [![Downloads](https://img.shields.io/pypi/dm/machina-ai.svg)](https://pypi.org/project/machina-ai/)
  [![Discord](https://img.shields.io/discord/000000000?label=Discord&logo=discord)](https://discord.gg/machina)

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

<!-- TODO: Add demo GIF here -->
<!-- <div align="center">
  <img src="docs/assets/demo.gif" alt="Machina in action" width="700"/>
</div> -->

## Why Machina?

Building an AI maintenance agent today means writing custom connectors for SAP PM, IBM Maximo, or whatever CMMS your plant uses. It means defining domain concepts like assets, work orders, and failure modes from scratch. It means handling OPC-UA subscriptions, Modbus registers, and MQTT topics. And it means engineering prompts that understand maintenance — all before writing a single line of business logic.

**That takes months. Machina makes it take minutes.**

Machina provides the missing vertical layer between general-purpose agent frameworks (LangChain, CrewAI, AutoGen) and the industrial maintenance world: pre-built connectors, a rich domain model based on ISO 14224, and maintenance-aware AI — so you can go from `pip install` to a working agent in under 30 minutes.

## Features

- **Industrial Connectors** — Pre-built integrations for CMMS (SAP PM, IBM Maximo, Limble), IoT protocols (OPC-UA, MQTT, Modbus), ERP systems, and communication platforms (Telegram, WhatsApp, Slack)
- **Maintenance Domain Model** — First-class Python objects for Asset, WorkOrder, FailureMode, SparePart, MaintenancePlan, and Alarm — with hierarchies, validation, and domain logic built in
- **Domain-Aware AI** — Agents that automatically resolve equipment references, inject maintenance context, retrieve relevant procedures, and ground answers in your data
- **Workflow Engine** — Composable multi-step workflows for common patterns like alarm-to-work-order, spare part checks, and maintenance scheduling
- **LLM-Agnostic** — Works with OpenAI, Anthropic, Mistral, Llama, Ollama, and any LiteLLM-compatible provider. No vendor lock-in
- **Async-First** — Built on `asyncio` for real-time sensor subscriptions, concurrent queries, and high-throughput production environments
- **MCP Server** — Expose any connector as an MCP server — let Claude Desktop, Cursor, or any MCP client query your CMMS and sensors without writing agent code
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

### Add Real-Time Sensor Monitoring

```python
from machina.connectors import OpcUa

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

See the [Architecture Guide](https://machina-ai.readthedocs.io/architecture) for details.

## Connectors

### CMMS

| Connector | System | Status |
|-----------|--------|--------|
| `GenericCmms` | Any REST-based CMMS | v0.1 |
| `SapPM` | SAP Plant Maintenance | v0.2 |
| `Maximo` | IBM Maximo | v0.2 |
| `UpKeep` | UpKeep CMMS | v0.2 |
| `MaintainX` | MaintainX | v0.2 |
| `Limble` | Limble CMMS | v0.2 |
| `Fiix` | Fiix (Rockwell) | v0.2 |
| `eMaint` | eMaint (Fluke) | v0.3 |
| `InforEam` | Infor EAM | v0.3 |

### IoT & Industrial Protocols

| Connector | Protocol | Status |
|-----------|----------|--------|
| `OpcUa` | OPC-UA | v0.2 |
| `Mqtt` | MQTT / Sparkplug B | v0.2 |
| `Modbus` | Modbus TCP/RTU | v0.3 |

### Communication

| Connector | Platform | Status |
|-----------|----------|--------|
| `Telegram` | Telegram Bot API | v0.1 |
| `WhatsApp` | WhatsApp Business | v0.2 |
| `Slack` | Slack Bot API | v0.2 |
| `Teams` | Microsoft Teams | v0.2 |
| `Email` | SMTP / IMAP / Gmail API | v0.2 |
| `GoogleChat` | Google Chat | v0.3 |

### Documents & Knowledge

| Connector | Source | Status |
|-----------|--------|--------|
| `DocumentStore` | PDF / DOCX with RAG | v0.1 |
| `Confluence` | Atlassian | v0.3 |
| `SharePoint` | Microsoft 365 | v0.3 |

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

See the [Custom Connectors Guide](https://machina-ai.readthedocs.io/connectors/custom) for the full tutorial.

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
)

# Define known failure modes
bearing_wear = FailureMode(
    code="BEAR-WEAR-01",
    name="Bearing Wear — Drive End",
    detection_methods=["vibration_analysis", "temperature_monitoring"],
    recommended_actions=["replace_bearing", "check_alignment"],
)
```

The domain model supports hierarchical asset trees, ISO 14224 failure taxonomies, work order lifecycle management, spare part inventory tracking, and maintenance plan scheduling.

See the [Domain Model Reference](https://machina-ai.readthedocs.io/domain) for all entities and services.

## Workflow Engine

Build multi-step maintenance workflows:

```python
from machina.workflows import Workflow, Step

alarm_to_workorder = Workflow(
    name="Alarm to Work Order",
    trigger="alarm",
    steps=[
        Step("diagnose", action="failure_analyzer.diagnose"),
        Step("check_history", action="cmms.get_asset_history"),
        Step("check_parts", action="inventory.check_availability"),
        Step("create_wo", action="work_order_factory.create"),
        Step("notify", action="telegram.send_message"),
    ],
)

agent.register_workflow(alarm_to_workorder)
```

## MCP Server

Don't need a full agent? Machina can expose its connectors as **MCP (Model Context Protocol) servers**, so Claude Desktop, Cursor, or any MCP-compatible tool can access your industrial data directly:

```bash
# Start MCP server exposing your CMMS and document store
machina mcp serve --config machina.yaml
```

Now anyone on your team can ask Claude: *"What's the maintenance history for pump P-201?"* — and Claude queries your SAP PM instance through Machina's MCP server. No agent code required.

This is also the fastest way to evaluate Machina: connect your data, use it from Claude, and when you need workflows and automation, the full framework is right there.

See the [MCP Server Guide](https://machina-ai.readthedocs.io/mcp-server) for setup instructions.

## Roadmap

- [x] Project specification and architecture design
- [x] Core domain model (Asset, WorkOrder, FailureMode, SparePart, Alarm)
- [x] BaseConnector protocol and ConnectorRegistry
- [x] Exception hierarchy
- [x] Configuration system (YAML + env var substitution)
- [x] LLM abstraction layer (LiteLLM wrapper)
- [x] Structured logging (structlog)
- [x] CI/CD pipeline (GitHub Actions)
- [x] GenericCmmsConnector
- [x] DocumentStore connector with RAG
- [x] Telegram connector + CLI channel
- [x] Agent runtime with domain-aware prompting
- [x] Entity resolver (natural language → asset resolution)
- [x] Action tracing (observability)
- [x] LLM tool definitions (function calling)
- [x] **v0.1 — Maintenance Knowledge Agent** (quickstart in 30 minutes)
- [ ] SAP PM, IBM Maximo, UpKeep, MaintainX connectors
- [ ] OPC-UA and MQTT connectors
- [ ] WhatsApp, Slack, Teams, Email connectors
- [ ] Workflow engine
- [ ] **MCP Server layer** — use connectors from Claude, Cursor, and any MCP client
- [ ] Plugin system for community extensions
- [ ] **v0.2 — Connectors, Workflows & MCP**
- [ ] Anomaly detection module
- [ ] Multi-agent orchestration
- [ ] **v0.3 — Intelligence & Scale**

See the [full roadmap](https://github.com/LGDiMaggio/machina/projects) for details.

## Examples

The [`examples/`](examples/) directory contains complete, runnable examples:

| Example | Description |
|---------|-------------|
| [`knowledge_agent/`](examples/knowledge_agent/) | Maintenance Knowledge Agent — the 30-minute quickstart |
| [`alarm_to_workorder/`](examples/alarm_to_workorder/) | Alarm-to-Work-Order workflow with CMMS integration |
| [`multi_agent_team/`](examples/multi_agent_team/) | Specialized agents collaborating on complex diagnostics |

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
- [Discord](https://discord.gg/machina) — Real-time chat with the community
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
