# 06 — YAML Configuration

Configure an agent entirely in YAML -- zero Python needed for basic setups.

## Run It

```bash
pip install machina-ai[litellm,docs-rag]
cd examples/06_yaml_config

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

## Python vs YAML

| Python (examples 01-05) | YAML (this example) |
|--------------------------|---------------------|
| Full control, custom logic | Declarative, no code |
| Workflows with guards and lambdas | Connectors + LLM + channels only |
| Best for: complex agents | Best for: standard deployments |

**Workflows stay in Python** -- they can contain lambdas, guard conditions, and custom logic that can't be expressed in YAML. Use `agent.register_workflow()` after `from_config()`:

```python
from machina import Agent
from machina.workflows.builtins import alarm_to_workorder

agent = Agent.from_config("machina.yaml")
agent.register_workflow(alarm_to_workorder)
agent.run()
```

## Next Steps

- [quickstart/](../quickstart/) -- Python-first approach
- [04_custom_workflows/](../04_custom_workflows/) -- Build workflows to register with your YAML-configured agent
