# Your First Maintenance Agent

Talk to your plant data in under 5 minutes.

## LLM Setup

Pick one:

### Option A: Ollama (local, free, no API key)

1. Install Ollama from [ollama.com](https://ollama.com)
2. Pull a model:
   ```bash
   ollama pull llama3
   ```
3. Verify it's running:
   ```bash
   ollama list
   ```

### Option B: OpenAI API

```bash
export OPENAI_API_KEY=sk-...
```

### Option C: Anthropic API

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Any [LiteLLM-compatible provider](https://docs.litellm.ai/docs/providers) works.

## Run It

```bash
pip install machina-ai[litellm]
cd examples/quickstart

# With Ollama (default):
python agent.py

# With OpenAI:
python agent.py --llm openai:gpt-4o

# With Anthropic:
python agent.py --llm anthropic:claude-sonnet-4-20250514

# With any Ollama model:
python agent.py --llm ollama:mistral
```

## The Code

```python
from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

agent = Agent(
    name="Maintenance Assistant",
    plant=Plant(name="Demo Plant"),
    connectors=[
        GenericCmmsConnector(data_dir="../sample_data/cmms"),
        DocumentStoreConnector(paths=["../sample_data/manuals"]),
    ],
    channels=[CliChannel()],
    llm="ollama:llama3",
)
agent.run()
```

That's the entire agent. 13 lines. No boilerplate.

## What a Conversation Looks Like

```
You: What is the bearing replacement procedure for pump P-201?

Agent: Based on the Grundfos CR 32-2 maintenance manual for pump P-201
(Cooling Water Pump), the bearing replacement procedure is:

1. Lock out/tag out the pump and isolate power supply
2. Drain the pump casing and disconnect piping
3. Remove the coupling guard and motor coupling
4. Remove the bearing housing cover bolts (6x M12)
5. Extract the drive-end bearing (SKF 6310-2RS) using a bearing puller
6. Clean the shaft journal and housing bore
7. Press-fit the new bearing using an induction heater (target: 110°C)
8. Reassemble in reverse order, torque cover bolts to 45 Nm
9. Verify vibration levels per ISO 10816-3 after commissioning

Recommended spare: SKF 6310-2RS (currently in stock: 4 units)

You: Are there spare parts available for the compressor?

Agent: For Compressor C-301 (Atlas Copco GA 55), the following spare
parts are tracked in inventory:

| Part           | SKU          | In Stock | Reorder Point |
|----------------|--------------|----------|---------------|
| Air filter     | AF-AC-GA55   | 6        | 3             |
| Oil filter     | OF-AC-GA55   | 4        | 2             |
| Separator elem.| SE-AC-GA55   | 2        | 1             |

All parts are above reorder point. No procurement action needed.

You: List all critical assets

Agent: Critical assets (Criticality A) in the plant:

1. P-201 — Cooling Water Pump (Grundfos CR 32-2) — Building A, Line 2
   - Equipment class: PU (ISO 14224)
   - 1 open corrective work order (WO-2026-0087)

2. C-301 — Main Air Compressor (Atlas Copco GA 55) — Building B
   - Equipment class: CO (ISO 14224)
   - Preventive maintenance due in 12 days
```

The agent resolves "the pump" to Asset P-201, retrieves context from your CMMS, searches equipment manuals via RAG, and answers in natural language. That's the domain model working for you.

## What's Happening Under the Hood

1. **Entity resolution** -- "the pump" or "P-201" maps to the actual Asset with its full metadata
2. **Context injection** -- maintenance history, active alarms, failure modes are gathered automatically from connectors
3. **RAG retrieval** -- equipment manuals are searched and relevant sections are injected into the LLM prompt
4. **Domain grounding** -- the LLM works with real Asset, WorkOrder, SparePart data, not hallucinated IDs

## Try Sandbox Mode

Want to test write operations safely? Sandbox mode logs every action without executing it:

```bash
python agent.py --sandbox
```

```
You: Create a work order for bearing replacement on P-201, priority HIGH

Agent: [SANDBOX] Work order WO-2026-0412 created:
  Type: PREDICTIVE | Priority: HIGH | Asset: P-201
  Description: Replace drive-end bearing (SKF 6310-2RS) based on
  elevated vibration readings exceeding ISO 10816-3 Zone B limit.
  Estimated duration: 4 hours
  Required skills: mechanical fitter, vibration analyst

  Note: Running in sandbox mode -- work order logged but not
  submitted to CMMS.
```

## Swap Your LLM

```bash
python agent.py --llm openai:gpt-4o
python agent.py --llm anthropic:claude-sonnet-4-20250514
python agent.py --llm ollama:mistral
```

No code changes. Machina uses [LiteLLM](https://github.com/BerriAI/litellm) under the hood.

## Connect Your Own Data

Replace the sample data with your systems:

```python
from machina.connectors import SapPM, OpcUA, Telegram

agent = Agent(
    connectors=[
        SapPM(url="https://sap.yourcompany.com/odata/v4", ...),
        OpcUA(endpoint="opc.tcp://plc:4840", ...),
        DocumentStore(paths=["./manuals/"]),
    ],
    channels=[Telegram(bot_token="...")],
    llm="openai:gpt-4o",
)
```

The agent logic doesn't change. Only the connectors do.

## Next Steps

| Example | What you'll learn |
|---------|-------------------|
| [01_alarm_response/](../01_alarm_response/) | Automate alarm-to-work-order with a built-in workflow |
| [02_predictive_pipeline/](../02_predictive_pipeline/) | Full autonomous pipeline: sensor to scheduled maintenance |
| [03_cmms_portability/](../03_cmms_portability/) | Same agent, different CMMS backends |
| [04_custom_workflows/](../04_custom_workflows/) | Build your own multi-step workflows |
