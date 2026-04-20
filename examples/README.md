# Machina Examples

Build AI agents for industrial maintenance. Two examples to get you from zero to automation.

## 1. Ask Your Plant Data (2 minutes)

```bash
pip install machina-ai[litellm,docs-rag]
cd examples/quickstart && python agent.py
```

The agent answers questions about equipment, procedures, spare parts, maintenance history -- grounded in real data via RAG. [Details &rarr;](quickstart/)

## 2. Automate: Alarm to Work Order (10 minutes)

```bash
cd examples/alarm_to_workorder && python agent.py
```

A vibration alarm fires on pump P-201. The agent diagnoses the failure, checks spare parts, creates a work order, and notifies the team. No human in the loop. [Details &rarr;](alarm_to_workorder/)

## 3. Deploy to Production (15 minutes)

```bash
cp -r templates/odl-generator-from-text my-agent
cd my-agent && cp .env.example .env && docker compose up
```

Clone-configure-deploy starter kit. Italian free-text messages become Work Orders. [Details &rarr;](../templates/odl-generator-from-text/)

---

## More Examples

Once you've run the above, explore specific patterns in [reference/](reference/):

- **Predictive pipeline** -- 10-step autonomous sensor-to-maintenance flow
- **CMMS portability** -- Same agent on SAP PM, Maximo, UpKeep
- **Custom workflows** -- Build any maintenance process as a workflow
- **YAML config** -- Zero-Python agent configuration
- **Agent-driven** -- Autonomous reasoning without predefined steps

## Sample Data

All examples share `sample_data/` -- a fictional manufacturing plant:

- **6 assets**: pumps, compressor, conveyor, motor, heat exchanger
- **5 work orders**: preventive + corrective maintenance
- **6 spare parts**: with inventory and reorder points
- **2 equipment manuals**: Grundfos pump, Atlas Copco compressor

## Prerequisites

```bash
pip install machina-ai[litellm,docs-rag]
```

**LLM provider** (pick one):

| Provider | Setup | Cost |
|----------|-------|------|
| **Ollama** | [ollama.com](https://ollama.com), then `ollama pull llama3` | Free, local |
| **OpenAI** | `export OPENAI_API_KEY=sk-...` | Pay-per-token |
| **Anthropic** | `export ANTHROPIC_API_KEY=sk-ant-...` | Pay-per-token |

All examples default to `ollama:llama3`. Override: `python agent.py --llm openai:gpt-4o`
