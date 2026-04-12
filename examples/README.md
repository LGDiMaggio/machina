# Machina Examples

Build AI agents for industrial maintenance. Start here.

## Your First Agent in 13 Lines

```python
from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

agent = Agent(
    name="Maintenance Assistant",
    plant=Plant(name="Demo Plant"),
    connectors=[
        GenericCmmsConnector(data_dir="sample_data/cmms"),
        DocumentStoreConnector(paths=["sample_data/manuals"]),
    ],
    channels=[CliChannel()],
    llm="ollama:llama3",
)
agent.run()
```

```bash
pip install machina-ai[litellm,docs-rag]
cd examples/quickstart && python agent.py
```

## Learning Path

```
Start Here           Automate              Go Deep
    |                   |                     |
    v                   v                     v
quickstart/  -->  01_alarm_response/  -->  02_predictive_pipeline/
  (5 min)            (15 min)                (30 min)

              03_cmms_portability/       04_custom_workflows/
                  (10 min)                  (20 min)
```

## Examples

| Example | What you'll build | Key concept |
|---------|-------------------|-------------|
| [**quickstart/**](quickstart/) | Interactive Q&A agent over CMMS data and manuals | Agent + connectors + LLM in 13 lines |
| [**01_alarm_response/**](01_alarm_response/) | Alarm triggers diagnosis, WO creation, team notification | Built-in workflow templates, sandbox mode |
| [**02_predictive_pipeline/**](02_predictive_pipeline/) | 10-step autonomous pipeline: sensor to scheduled maintenance | Custom workflows, 3 LLM + 7 deterministic steps |
| [**03_cmms_portability/**](03_cmms_portability/) | Same agent works across SAP PM, Maximo, UpKeep | Connector abstraction, domain model portability |
| [**04_custom_workflows/**](04_custom_workflows/) | Spare part reorder + preventive scheduling workflows | Workflow DSL: triggers, guards, error policies |
| [**05_multi_agent_team/**](05_multi_agent_team/) | Specialist agents collaborate on diagnostics | Multi-agent orchestration (v0.3) |

## Which Example is for Me?

**"I want to see it work"** --> [quickstart/](quickstart/)

**"I need to automate a maintenance process"** --> [01_alarm_response/](01_alarm_response/)

**"I want full autonomous predictive maintenance"** --> [02_predictive_pipeline/](02_predictive_pipeline/)

**"I'm a system integrator deploying across multiple clients"** --> [03_cmms_portability/](03_cmms_portability/)

**"I want to build my own workflows"** --> [04_custom_workflows/](04_custom_workflows/)

## Interactive Tour

For a hands-on walkthrough in Jupyter, see [tour.ipynb](tour.ipynb) -- covers all the above in one notebook.

## Sample Data

All examples share `sample_data/` -- a fictional manufacturing plant:

- **6 assets**: pumps, compressor, conveyor, motor, heat exchanger
- **5 work orders**: preventive + corrective maintenance
- **6 spare parts**: with inventory and reorder points
- **2 equipment manuals**: Grundfos pump, Atlas Copco compressor
- **Sensor readings**: vibration + temperature time series for pump P-201

## Prerequisites

### Install

```bash
pip install machina-ai[litellm,docs-rag]
```

- `litellm` — LLM provider abstraction (required for all examples)
- `docs-rag` — document search with ChromaDB (used by all examples for manual search)

### LLM Provider Setup

Every example needs one LLM provider. Pick one:

| Provider | Setup | Cost |
|----------|-------|------|
| **Ollama** | Install from [ollama.com](https://ollama.com), then `ollama pull llama3` | Free, runs locally |
| **OpenAI** | `export OPENAI_API_KEY=sk-...` ([get key](https://platform.openai.com/api-keys)) | Pay-per-token |
| **Anthropic** | `export ANTHROPIC_API_KEY=sk-ant-...` ([get key](https://console.anthropic.com/)) | Pay-per-token |

All examples default to `ollama:llama3`. Override with `--llm`:

```bash
python agent.py                              # default: ollama:llama3
python agent.py --llm openai:gpt-4o          # requires OPENAI_API_KEY
python agent.py --llm anthropic:claude-sonnet-4-20250514  # requires ANTHROPIC_API_KEY
python agent.py --llm ollama:mistral          # any Ollama model
```
