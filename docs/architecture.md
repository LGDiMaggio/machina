# Architecture

Machina is a layered framework for building industrial maintenance AI agents.
Each layer has a single responsibility, talks to its neighbours through a stable
contract, and can be swapped or extended without touching the others.

## The five layers

### 1. Connectors

The outermost layer. Every connector talks to exactly one external system —
a CMMS, an IoT protocol, an ERP, or a messaging channel — and **normalizes its
responses into domain entities**. Connectors never return raw API payloads to
the rest of the framework.

Connectors declare their capabilities as a simple list (`["read_assets",
"read_work_orders", "create_work_order"]`) so the agent runtime can discover at
runtime what actions are available and enable the matching LLM tools.

Available connectors: **CMMS** — `GenericCmmsConnector` (local JSON + REST),
`SapPmConnector`, `MaximoConnector`, `UpKeepConnector`; **IoT** —
`OpcUaConnector`, `MqttConnector`; **Documents** — `DocumentStoreConnector`
(manuals with keyword-fallback or ChromaDB-backed RAG); **Communication** —
`TelegramConnector`, `SlackConnector`, `EmailConnector`, `CalendarConnector`,
`CliChannel`.

### 2. Domain Model

The backbone of the framework. Every connector normalizes into these entities,
and every agent reasons in these terms. The model is aligned with **ISO 14224** —
the international standard for reliability and maintenance data collection — so
teams already using ISO-aligned CMMS tooling can interoperate without custom
field mapping.

The entities are pydantic v2 models with validators: `Asset`, `WorkOrder`,
`FailureMode`, `SparePart`, `Alarm`, `MaintenancePlan`, and `Plant`. See
[Domain Model Reference](domain.md) for the full API.

### 3. Agent Runtime

The `Agent` class orchestrates everything. It owns a `Plant` (the asset registry),
a `ConnectorRegistry` (the connectors it can talk to), an `LLMProvider`, an
`EntityResolver`, and a list of communication channels. Channels are also
registered into the `ConnectorRegistry` so capability-based dispatch (e.g.
workflow `channels.send_message` steps) reaches them alongside other connectors.
When a message comes in,
the runtime:

1. Resolves named entities in the user's text against the plant registry
2. Gathers context from every registered connector in parallel (via `asyncio.gather`)
3. Builds a grounded system prompt with the retrieved context injected
4. Calls the LLM with tools, executes any tool calls the LLM emits, and loops
5. Returns the final response to the channel

### 4. LLM Abstraction

A thin wrapper over [LiteLLM](https://github.com/BerriAI/litellm). Machina does
**not** build its own LLM abstraction — LiteLLM already supports 100+ providers
(OpenAI, Anthropic, Ollama, Mistral, Mistral AI, Cohere, …). The `LLMProvider`
class adds maintenance-aware defaults on top, nothing more.

Switching providers is a one-line change: `llm="openai:gpt-4o"` →
`llm="ollama:llama3:8b"`. The agent runtime handles both provider response
shapes transparently (`tool_calls=None` vs `tool_calls=[]`).

### 5. Observability

Structured logging via [structlog](https://www.structlog.org/) + an `ActionTracer`
that records every agent action (connector calls, LLM invocations, tool
executions) for debugging and auditing. Every log line carries structured
context: `connector=`, `asset_id=`, `operation=`.

## Data flow

A user asks *"What's the status of pump P-201?"*. Here's what happens:

```text
User message
    │
    ▼
Channel (CliChannel / TelegramConnector)
    │  IncomingMessage { text, chat_id }
    ▼
Agent.handle_message()
    │
    ├──► EntityResolver.resolve("... pump P-201 ...")
    │    └── matches Asset(id="P-201")  [exact_id, confidence=1.0]
    │
    ├──► _gather_context(text, resolved) — parallel connector calls
    │    ├── CMMS.read_work_orders(asset_id="P-201")
    │    ├── CMMS.read_spare_parts(asset_id="P-201")
    │    └── DocumentStore.search("pump P-201", asset_id="P-201")
    │
    ├──► _build_messages() — inject everything as a system message
    │    └── "Retrieved context: Asset P-201 (Cooling Water Pump,
    │         Grundfos CR 32-2), 3 open WOs, 2 compatible parts,
    │         2 manual excerpts..."
    │
    ├──► _llm_loop() — LLM call with BUILTIN_TOOLS
    │    ├── LLM may call tools (search_assets, read_work_orders, …)
    │    └── Final text response
    │
    ▼
Response → Channel → User
```

## See also

- **[Domain Model Reference](domain.md)** — Every class, field, and validator
- **[Custom Connectors](connectors/custom.md)** — How to plug in a new system
- **[MCP Server](mcp-server.md)** — Exposing connectors to Claude Desktop, Cursor, etc.
