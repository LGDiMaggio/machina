# 06 — YAML Configuration

Configure an agent entirely in YAML -- zero Python needed for basic setups.

## Run It

```bash
pip install machina-ai[litellm,docs-rag]
cd examples/reference/yaml_config

# Default (Ollama):
python agent.py

# With a different config file:
python agent.py --config machina_openai.yaml

# Override LLM from CLI:
python agent.py --llm anthropic:claude-sonnet-4-20250514
```

> See [examples/README.md](../README.md#prerequisites) for LLM provider setup (Ollama, OpenAI, Anthropic).

## The Config File

```yaml
name: "Maintenance Assistant"

plant:
  name: "North Plant"
  location: "Building A"

connectors:
  cmms:
    type: generic_cmms
    settings:
      data_dir: "../sample_data/cmms"
  docs:
    type: document_store
    settings:
      paths:
        - "../sample_data/manuals"

channels:
  - type: cli

llm:
  provider: "ollama:llama3"
  temperature: 0.1

sandbox: false
```

## The Agent Script

```python
from machina import Agent

agent = Agent.from_config("machina.yaml")
agent.run()
```

That's it. Two lines.

## What You Can Configure

| Section | What it does |
|---------|-------------|
| `name` / `description` | Agent identity |
| `plant` | Plant name and location |
| `connectors` | Named connector instances with type + settings |
| `channels` | Communication channels (`cli`, `telegram`, `slack`, `email`) |
| `llm` | LLM provider, temperature, max tokens |
| `sandbox` | Safe mode -- write actions are logged, not executed |

## Available Connector Types

| Type | Class | Extra needed |
|------|-------|-------------|
| `generic_cmms` | GenericCmmsConnector | -- |
| `sap_pm` | SapPmConnector | `cmms-rest` |
| `maximo` | MaximoConnector | `cmms-rest` |
| `upkeep` | UpKeepConnector | `cmms-rest` |
| `opcua` | OpcUaConnector | `opcua` |
| `mqtt` | MqttConnector | `mqtt` |
| `document_store` | DocumentStoreConnector | `docs-rag` |
| `telegram` | TelegramConnector | `telegram` |
| `slack` | SlackConnector | `slack` |
| `email` | EmailConnector | -- |
| `calendar` | CalendarConnector | `calendar` |
| `simulated_sensor` | SimulatedSensorConnector | -- |

## Environment Variables

Use `${VAR}` syntax in YAML for secrets:

```yaml
connectors:
  sap:
    type: sap_pm
    settings:
      url: "https://sap.company.com/odata/v4"
      auth:
        token: "${SAP_TOKEN}"
```

Then set the variable before running:

```bash
export SAP_TOKEN=eyJhbGci...
python agent.py
```

## When to Use YAML vs Python

YAML config is designed for **knowledge-base agents** — the kind that answer technician questions over CMMS data, equipment manuals, and spare parts. No custom automation, just Q&A.

For **agents with workflows** (alarm response, predictive pipelines), use Python. Workflows need guards, lambdas, and error policies that YAML can't express. This is by design — encoding Python logic in YAML would be fragile and hard to debug.

| | YAML (this example) | Python (examples 01-05) |
|-|---------------------|--------------------------|
| **Agent type** | Knowledge-base / Q&A | Workflow automation |
| **Configures** | Connectors, LLM, channels, plant | Everything + workflows |
| **Best for** | Standard deployments, Docker, ops | Complex agents, custom logic |

You can also combine both — YAML for infra, Python for workflows:

```python
from machina import Agent
from machina.workflows.builtins import alarm_to_workorder

agent = Agent.from_config("machina.yaml")
agent.register_workflow(alarm_to_workorder)
agent.run()
```

## Next Steps

- [quickstart/](../../quickstart/) -- Python-first approach
- [custom_workflows/](../custom_workflows/) -- Build workflows to register with your YAML-configured agent
