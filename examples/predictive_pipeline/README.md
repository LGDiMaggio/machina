# Predictive Maintenance Pipeline — Full Workflow Example

This example demonstrates Machina's most advanced capability: an **autonomous predictive maintenance pipeline** that goes from sensor alarm to scheduled work order — without human intervention.

Unlike the [Knowledge Agent](../knowledge_agent/) (a Q&A chatbot), this example showcases Machina as an **agentic system** that detects, diagnoses, acts, and optimizes on its own.

## What It Does

When a sensor reading crosses a threshold (e.g., vibration on pump P-201 exceeds 6 mm/s), the pipeline automatically:

1. **Detects** — enriches the alarm with correlated sensor readings from the same asset
2. **Diagnoses** — combines rule-based failure mode analysis with LLM-powered root cause reasoning and RAG retrieval from maintenance manuals
3. **Acts** — checks spare parts availability, creates a prioritized work order in the CMMS
4. **Optimizes** — finds the best maintenance window considering production constraints
5. **Notifies** — sends a structured alert to the maintenance team via Telegram (or CLI)

## What's Included

- **Workflow definition**: a 10-step pipeline with deterministic + LLM-powered steps
- **Sample sensor data**: simulated vibration and temperature readings for pump P-201
- **Shared CMMS/manuals data**: reuses the `knowledge_agent` sample data (assets, work orders, spare parts, manuals)
- **Sandbox mode**: run the full pipeline with all actions logged but not executed

## Quick Start

### 1. Install Machina

```bash
pip install machina-ai[litellm]
```

Or from the repo root:

```bash
pip install -e ".[dev,litellm]"
```

### 2. Set your API key

```bash
export OPENAI_API_KEY=your_key_here
```

### 3. Run the pipeline

```bash
cd examples/predictive_pipeline
python main.py
```

This simulates a vibration alarm on pump P-201 and runs the full pipeline.

### 4. Try sandbox mode

```bash
python main.py --sandbox
```

In sandbox mode, every action is logged to the console with full detail, but nothing is actually written to the CMMS or sent via Telegram. Perfect for understanding the pipeline before connecting real systems.

## Using with Different LLMs

```bash
# Ollama (local, private — no data leaves your network)
python main.py --llm ollama:llama3

# Anthropic Claude
export ANTHROPIC_API_KEY=your_key_here
python main.py --llm anthropic:claude-sonnet-4-20250514

# Mistral
export MISTRAL_API_KEY=your_key_here
python main.py --llm mistral:mistral-large-latest
```

## Using with Telegram

```bash
export TELEGRAM_BOT_TOKEN=your_bot_token
python main.py --telegram
```

## Architecture

```
Sensor alarm (OPC-UA / MQTT / simulated)
    │
    ▼ Alarm domain entity
┌─────────────────────────────────────────────────────────────┐
│                  WORKFLOW ENGINE                             │
│                                                             │
│  Phase 1: DETECTION                                         │
│  ┌──────────────────┐                                       │
│  │ enrich_alarm      │ → read correlated sensors             │
│  └────────┬─────────┘                                       │
│           ▼                                                  │
│  Phase 2: DIAGNOSIS                                          │
│  ┌──────────────────┐                                       │
│  │ diagnose_rules    │ → FailureAnalyzer (deterministic)     │
│  │ search_manuals    │ → DocumentStore RAG                   │
│  │ diagnose_llm      │ → Agent reasons on all evidence  🤖  │
│  └────────┬─────────┘                                       │
│           ▼                                                  │
│  Phase 3: ACTION                                             │
│  ┌──────────────────┐                                       │
│  │ check_parts       │ → SparePart availability              │
│  │ check_history     │ → WorkOrder history from CMMS         │
│  │ draft_wo          │ → Agent writes WO description    🤖  │
│  │ submit_wo         │ → WorkOrderFactory → CMMS             │
│  └────────┬─────────┘                                       │
│           ▼                                                  │
│  Phase 4: OPTIMIZATION                                       │
│  ┌──────────────────┐                                       │
│  │ find_window       │ → MaintenanceScheduler                │
│  │ optimize_schedule │ → Agent optimizes timing         🤖  │
│  └────────┬─────────┘                                       │
│           ▼                                                  │
│  ┌──────────────────┐                                       │
│  │ notify_team       │ → Telegram / CLI message              │
│  └──────────────────┘                                       │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
  Technician receives structured alert with diagnosis,
  work order ID, spare parts status, and scheduled window
```

**🤖 = step where the LLM agent participates** (3 out of 10 steps). The other 7 steps are deterministic — fast, predictable, and testable without an LLM.

## How It Differs from the Knowledge Agent

| | Knowledge Agent | Predictive Pipeline |
|---|---|---|
| **Mode** | Reactive (Q&A) | Autonomous (event-driven) |
| **Trigger** | Human question | Sensor alarm |
| **LLM usage** | Every interaction | Only 3 out of 10 steps |
| **Output** | Text answer | Work order + schedule + notification |
| **Domain services** | Entity resolution | FailureAnalyzer + WorkOrderFactory + MaintenanceScheduler |
| **Workflow** | None | 10-step pipeline with context propagation |

## Sample Data

```
sample_data/
└── sensor_logs/
    └── pump_p201_readings.json  # Simulated vibration + temperature time series

../knowledge_agent/sample_data/   # Shared — reused from the knowledge_agent example
├── cmms/
│   ├── assets.json
│   ├── work_orders.json
│   └── spare_parts.json
└── manuals/
    ├── pump_p201_manual.md
    └── compressor_comp301_manual.md
```

## Adapting to Your Environment

To connect this pipeline to real systems, replace the sample connectors:

```python
# Instead of GenericCmmsConnector with local data:
from machina.connectors import SapPM
cmms = SapPM(url="https://sap.yourcompany.com/odata/v4", ...)

# Instead of SimulatedSensorConnector:
from machina.connectors import OpcUa
sensors = OpcUa(endpoint="opc.tcp://plc-line2:4840", subscriptions=[...])

# Instead of CliChannel:
from machina.connectors import Telegram
channel = Telegram(bot_token="...")
```

The workflow definition stays exactly the same — that's the power of Machina's domain model abstraction.
