# Knowledge Agent — Quickstart Example

This example demonstrates Machina's core capability: a maintenance knowledge agent that can answer questions about plant equipment using data from a CMMS and technical documents.

## What's Included

- **Sample CMMS data**: 6 assets, 5 work orders, 6 spare parts (JSON files)
- **Sample manuals**: Maintenance procedures for a pump and a compressor (Markdown)
- **CLI mode**: Interactive terminal interface — no Telegram setup required
- **Telegram mode**: Optional — connect to a real Telegram bot

## Quick Start (< 5 minutes)

### 1. Install Machina

```bash
pip install machina-ai[litellm]
```

Or from the repo root:

```bash
pip install -e ".[dev,litellm]"
```

### 2. Set your OpenAI API key

```bash
export OPENAI_API_KEY=your_key_here
```

### 3. Run the agent

```bash
cd examples/knowledge_agent
python main.py
```

### 4. Try these questions

```
You> What equipment do we have in Building A?
You> Tell me about pump P-201
You> What's the bearing replacement procedure for P-201?
You> Are there spare bearings in stock for P-201?
You> What work orders are open for the compressor?
You> The pump in building A has high vibration, what could be wrong?
```

## Using with Telegram

```bash
export TELEGRAM_BOT_TOKEN=your_bot_token
python main.py --telegram
```

## Using with Ollama (local LLM)

```bash
python main.py --llm ollama:llama3
```

## Using with Anthropic Claude

```bash
export ANTHROPIC_API_KEY=your_key_here
python main.py --llm anthropic:claude-sonnet-4-20250514
```

## Sample Data Structure

```
sample_data/
├── cmms/
│   ├── assets.json          # 6 plant assets (pumps, compressor, conveyor, etc.)
│   ├── work_orders.json     # 5 work orders (preventive and corrective)
│   └── spare_parts.json     # 6 spare parts with inventory levels
└── manuals/
    ├── pump_p201_manual.md         # Detailed pump maintenance manual
    └── compressor_comp301_manual.md # Compressor maintenance manual
```

## Architecture

```
User question
    │
    ▼
┌──────────────┐
│ Entity       │  "pump in building A" → Asset P-201
│ Resolver     │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Context      │  Fetch work orders, spare parts, documents
│ Gathering    │  for the resolved asset
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ LLM +        │  Domain-aware system prompt + retrieved context
│ Tools        │  + tool calls for additional data
└──────┬───────┘
       │
       ▼
  Grounded response with source references
```
