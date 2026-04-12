# Quickstart

This guide walks you through building a **Maintenance Knowledge Agent** end-to-end
in about 30 minutes. The agent ingests sample CMMS data (assets, work orders, spare
parts), loads a couple of equipment manuals, and answers technician questions about
them over a CLI chat — no external accounts required to try it out.

## Prerequisites

- **Python 3.11+** (`python --version`)
- **One LLM provider** — either:
    - An OpenAI API key in `OPENAI_API_KEY` (recommended for first run), **or**
    - A local [Ollama](https://ollama.ai/) install with a model pulled (e.g. `ollama pull llama3`)

You do **not** need SAP, Maximo, or any real CMMS installed. The quickstart uses
the sample data that ships with the `examples/sample_data/` folder.

## 1. Install

```bash
pip install machina-ai[litellm,docs-rag]
```

- `litellm` pulls in the LLM provider abstraction (OpenAI, Anthropic, Ollama, …).
- `docs-rag` pulls in LangChain document loaders + ChromaDB for the sample manuals.

## 2. Write the agent

Create `knowledge_agent.py`:

```python
from pathlib import Path

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector

# Point at the sample data that ships with the examples folder
sample_dir = Path("examples/sample_data")

cmms = GenericCmmsConnector(data_dir=sample_dir / "cmms")
docs = DocumentStoreConnector(paths=[sample_dir / "manuals"])

agent = Agent(
    name="Maintenance Knowledge Agent",
    description="Answers questions about plant equipment, maintenance history, and procedures.",
    plant=Plant(name="North Plant"),
    connectors=[cmms, docs],
    channels=[CliChannel()],
    llm="openai:gpt-4o",  # or "ollama:llama3"
)

agent.run()
```

## 3. Run it

```bash
python knowledge_agent.py
```

You'll get an interactive prompt. Try:

```text
> What's the maintenance history of pump P-201?
> Which spare parts are compatible with the Grundfos CR 32-2?
> How do I replace the bearing on the drive-end side?
```

The agent resolves "pump P-201" to the asset in the sample CMMS, pulls its work
order history, searches the pump manual for the bearing replacement procedure,
and returns a grounded answer with citations.

## 4. What just happened?

Behind that single `agent.run()` call, Machina did the following for each question:

1. **Entity resolution** — The `EntityResolver` matched "P-201" against the assets
   loaded from the sample CMMS (`Asset.id == "P-201"`, exact-ID match, confidence 1.0).
2. **Context gathering** — The agent queried every registered connector in parallel:
   `GenericCmmsConnector.read_work_orders(asset_id="P-201")` for the history,
   `DocumentStoreConnector.search("bearing replacement")` for the manual section.
3. **Grounded prompt** — The retrieved asset metadata, work orders, and document
   chunks were injected into the LLM's system message via `build_context_message`,
   so the LLM's answer is grounded in real plant data — not hallucinated.

## Next steps

- **[Architecture](architecture.md)** — Understand the five layers (connectors,
  domain, agent, LLM, observability) and how they compose.
- **[Domain Model Reference](domain.md)** — Explore `Asset`, `WorkOrder`, `FailureMode`,
  and the rest of the ISO 14224-aligned entities.
- **[Custom Connectors](connectors/custom.md)** — Build a connector for your own
  CMMS or sensor system using the `BaseConnector` Protocol.
- **Run the real example** — `python examples/quickstart/agent.py --llm openai:gpt-4o`
  is the full version of the script above, with CLI args for LLM selection, verbose
  logging, and sandbox mode.
