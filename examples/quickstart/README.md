# Your First Maintenance Agent

Talk to your plant data in under 5 minutes.

## LLM Setup

Pick one:

### Option A: Ollama (local, free, no API key) — default

1. Install Ollama from [ollama.com](https://ollama.com)
2. Pull the default model — `llama3` (8B):
   ```bash
   ollama pull llama3
   ```
3. Verify it's running:
   ```bash
   ollama list
   ```

The quickstart defaults to `ollama:llama3`. It runs on CPU — the first
answer can take ~20–40s, and questions that trigger a tool call (like
creating a work order) take a little longer; a GPU or a hosted key (Options
B/C) is noticeably snappier.

> **Why an 8B model?** The agent relies on tool calling, multi-step
> synthesis, and a citation contract. `llama3` is the smallest model that
> handles all three in most turns — not every turn: in test runs it
> occasionally returned a tool instruction instead of an answer, which the
> agent reports instead of showing. Smaller models are tempting for speed,
> but 3–4B ones (e.g. `qwen2.5:3b`, `qwen3:4b`) often can't keep the
> contract and return empty or raw-context answers — so they're listed as
> fast-but-may-struggle options, not the default. A hosted model (Options
> B/C) or a larger local one is noticeably more reliable. Pass any tool-calling model you prefer with `--llm`
> (e.g. `--llm ollama:qwen2.5:7b`).

**On a machine with a GPU?** Pull a larger model for noticeably better
answers and just point the example at it:

```bash
ollama pull qwen2.5:14b
python agent.py --llm ollama:qwen2.5:14b   # or ollama:qwen3:14b
```

Ollama uses the GPU automatically — Machina adds no device or GPU
configuration of its own. Same code, same command, the larger model just
loads onto the GPU.

### Option B: OpenAI API

Easiest: copy `examples/.env.example` to `examples/.env` and fill in the key.
The preflight loads it automatically via `python-dotenv`.

```
OPENAI_API_KEY=sk-...
```

Or set it as a shell env var for the current session:

```bash
# macOS / Linux
export OPENAI_API_KEY=sk-...

# Windows (PowerShell)
$env:OPENAI_API_KEY = "sk-..."

# Windows (CMD)
set OPENAI_API_KEY=sk-...
```

### Option C: Anthropic API

Same `.env` approach:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Or as a shell env var:

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=sk-ant-...

# Windows (PowerShell)
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# Windows (CMD)
set ANTHROPIC_API_KEY=sk-ant-...
```

Any [LiteLLM-compatible provider](https://docs.litellm.ai/docs/providers) works.

## Run It

The examples live in the repo (not in the published wheel), so clone first:

```bash
git clone https://github.com/LGDiMaggio/machina.git
cd machina
pip install -e ".[litellm,docs-rag,examples]"
cd examples/quickstart

# With Ollama (default: ollama:llama3):
python agent.py

# With a larger model (GPU recommended):
python agent.py --llm ollama:qwen2.5:14b

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
from machina.connectors.comms.cli import CliChannel
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

An example session, abridged (the wording depends on the model):

```
You: What is the bearing replacement procedure for pump P-201?

Agent: From the Grundfos CR 32-2 manual for pump P-201 (Cooling Water Pump) [1]:

1. Lock out / tag out the motor and verify zero energy state
2. Disconnect the coupling
3. Remove the bearing housing covers (4x M12 bolts)
4. Support the shaft and pull the old bearings
5. Clean the shaft journals with solvent; inspect for scoring (max 0.01 mm)
6. Heat the new bearings to 110 °C with an induction heater
7. Slide them onto the shaft and let them cool naturally (no water quench)
8. Pack the housings with SKF LGMT 3 grease (30–40 % of the cavity)
9. Refit the covers, torque the bolts to 80 Nm in a star pattern
10. Verify alignment, reconnect the coupling, remove LOTO
11. Start the pump and check: no abnormal noise, vibration < 2.5 mm/s,
    bearing temperature stable < 60 °C after 1 hour

Parts: 2x SKF 6310 deep groove ball bearing (currently in stock: 4).

— Sources:
  • [1] pump_p201_manual.md

You: Create a work order for bearing replacement on P-201, priority HIGH

⚠️  Create a work order?
  • Asset: P-201
  • Type: corrective
  • Priority: high
  • Description: Replace the drive-end bearings (SKF 6310) following the manual procedure; elevated vibration on the drive-end bearing.
Confirm? [y/N] y

Agent: Created work order WO-AUTO-0B2E8887 on P-201 (corrective, priority high).

You: List all critical assets

Agent: Critical assets (criticality A):

1. P-201 — Cooling Water Pump — Building A / Line 2 / Cooling System
2. COMP-301 — Air Compressor Unit 1 — Building B / Utilities / Compressed Air
3. CONV-101 — Main Production Conveyor — Building A / Line 1 / Assembly
4. MOT-201A — Pump P-201 Drive Motor — Building A / Line 2 / Cooling System
```

The agent resolves "the pump" to Asset P-201, retrieves context from your CMMS, searches equipment manuals via RAG (note the inline `[1]` citations and the `— Sources` footer tracing each claim back to the manual), and answers in natural language. That's the domain model working for you.

Notice the **confirmation gate**: the agent did not silently create the work order. It proposed the exact write — asset, type, priority, description — and only executed it after you typed `y`. Type anything else (or just press Enter) and the write is declined. This gate is on by default; see [Safety: the confirmation gate](#safety-the-confirmation-gate) below.

## What's Happening Under the Hood

1. **Entity resolution** -- "the pump" or "P-201" maps to the actual Asset with its full metadata; a weak or ambiguous match makes the agent ask which asset you mean
2. **Context injection** -- once the asset is resolved, its work orders and compatible spare parts are fetched from the CMMS automatically
3. **RAG retrieval** -- equipment manuals are searched for your question and the relevant sections are injected into the LLM prompt
4. **Domain grounding** -- the LLM works with real Asset, WorkOrder, SparePart data, and calls tools for anything else it needs (asset search, work orders, spare parts, diagnosis)

## Safety: the confirmation gate

The agent never performs a write — creating a work order, triggering a
workflow — without your explicit go-ahead. When the model decides to call a
mutating tool, the runtime pauses and prints the **concrete** proposed
action, then waits for a `y`:

```
You: Create a work order for bearing replacement on P-201, priority HIGH

⚠️  Create a work order?
  • Asset: P-201
  • Type: corrective
  • Priority: high
  • Description: Replace the drive-end bearings (SKF 6310) following the manual procedure; elevated vibration on the drive-end bearing.
Confirm? [y/N]
```

Type `y` (or `yes`) to proceed; anything else — including an empty line —
declines, and nothing is written. This is **on by default** and is the
quickstart's safety story: you see exactly what the agent will do before it
happens.

### Sandbox vs. live (testing aid)

Separately, `--sandbox` lets you dry-run write logic — every action is
logged, none executed — which is handy when developing or demoing without a
live CMMS:

```bash
python agent.py --sandbox    # log-only (writes are no-ops)
python agent.py --live       # execute writes (default)
```

The confirmation gate, not sandbox mode, is the headline guard against
unintended writes; sandbox is a developer testing convenience.

## Swap Your LLM

```bash
python agent.py --llm ollama:qwen2.5:14b   # larger local Qwen (GPU)
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

- [**alarm_to_workorder/**](../alarm_to_workorder/) -- Automate: alarm fires, agent creates a work order (10 min)
- [**Starter kit**](../../templates/odl-generator-from-text/) -- Copy, configure, run: free-text requests to confirmed work orders (Python or Docker)
- [**More examples**](../reference/) -- Predictive pipelines, custom workflows, CMMS portability
