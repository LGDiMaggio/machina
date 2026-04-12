# YAML Configuration

Configure a Machina agent declaratively -- no Python needed for standard deployments.

## Quick Example

Create `machina.yaml`:

```yaml
name: "Maintenance Assistant"

plant:
  name: "North Plant"
  location: "Building A"

connectors:
  cmms:
    type: generic_cmms
    settings:
      data_dir: "./data/cmms"
  docs:
    type: document_store
    settings:
      paths: ["./data/manuals"]

channels:
  - type: cli

llm:
  provider: "ollama:llama3"
  temperature: 0.1

sandbox: false
```

Then load it:

```python
from machina import Agent

agent = Agent.from_config("machina.yaml")
agent.run()
```

## Configuration Reference

### Top-level fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `"Machina Agent"` | Agent name |
| `description` | string | `"Maintenance AI assistant"` | Agent description |
| `plant` | object | `{name: "Default Plant"}` | Plant configuration |
| `connectors` | object | `{}` | Named connector instances |
| `channels` | list | `[]` (defaults to CLI) | Communication channels |
| `llm` | object | `{provider: "ollama:llama3"}` | LLM provider settings |
| `sandbox` | boolean | `false` | Enable sandbox mode (writes logged, not executed) |

### Plant

```yaml
plant:
  name: "North Plant"
  location: "Building A"
```

### Connectors

Each connector has a `type`, optional `enabled` flag, and a `settings` dict:

```yaml
connectors:
  my_cmms:
    type: generic_cmms
    enabled: true          # default: true
    settings:
      data_dir: "./data/cmms"
```

#### Available types

| Type | Class | Extra |
|------|-------|-------|
| `generic_cmms` | GenericCmmsConnector | -- |
| `sap_pm` | SapPmConnector | `cmms-rest` |
| `maximo` | MaximoConnector | `cmms-rest` |
| `upkeep` | UpKeepConnector | `cmms-rest` |
| `opcua` | OpcUaConnector | `opcua` |
| `mqtt` | MqttConnector | `mqtt` |
| `document_store` | DocumentStoreConnector | `docs-rag` |
| `simulated_sensor` | SimulatedSensorConnector | -- |
| `telegram` | TelegramConnector | `telegram` |
| `slack` | SlackConnector | `slack` |
| `email` | EmailConnector | -- |
| `calendar` | CalendarConnector | `calendar` |

The `settings` dict is passed as keyword arguments to the connector constructor.
Check each connector's documentation for available settings.

### Channels

```yaml
channels:
  - type: cli
  - type: telegram
    settings:
      bot_token: "${BOT_TOKEN}"
```

If `channels` is empty or omitted, a CLI channel is used by default.

### LLM

```yaml
llm:
  provider: "ollama:llama3"     # or "openai:gpt-4o", "anthropic:claude-sonnet-4-20250514"
  temperature: 0.1
  max_tokens: 4096
```

## Environment Variables

Use `${VAR}` syntax anywhere in the YAML. Variables are resolved at load time:

```yaml
connectors:
  sap:
    type: sap_pm
    settings:
      url: "https://sap.company.com/odata/v4"
      auth:
        token: "${SAP_TOKEN}"
```

```bash
export SAP_TOKEN=eyJhbGci...
python agent.py
```

If a referenced variable is not set, `load_config()` raises `ValueError`
with a clear message.

## Adding Workflows

Workflows contain Python callables (lambdas, guard conditions) that can't be
expressed in YAML. Register them after loading:

```python
from machina import Agent
from machina.workflows.builtins import alarm_to_workorder

agent = Agent.from_config("machina.yaml")
agent.register_workflow(alarm_to_workorder)
agent.run()
```

## Python vs YAML

| | Python | YAML |
|-|--------|------|
| **Control** | Full -- custom logic, lambdas, guards | Declarative -- connectors, LLM, channels |
| **Workflows** | Yes -- full DSL | Register after `from_config()` |
| **Best for** | Complex agents, custom integrations | Standard deployments, Docker, ops teams |
| **Example** | [quickstart/](../examples/quickstart/) | [06_yaml_config/](../examples/06_yaml_config/) |

## Next Steps

- [Quickstart](quickstart.md) -- Python-first approach
- [Custom Connectors](connectors/custom.md) -- Build your own connector (works with both Python and YAML)
- [Architecture](architecture.md) -- Understand the five layers
