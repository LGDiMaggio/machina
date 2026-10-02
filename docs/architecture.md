# Architecture

Machina is a layered framework for building industrial maintenance AI agents.
Each layer has a single responsibility, talks to its neighbours through a stable
contract, and can be swapped or extended without touching the others.

## The layers

### 1. Connectors

The outermost layer. Every connector talks to exactly one external system —
a CMMS, a spreadsheet or database, an IoT protocol, a document store, or a
messaging channel — and **normalizes its responses into domain entities**.
Connectors never return raw API payloads to the rest of the framework.

Each connector class declares what it can do as a typed set,
`capabilities: ClassVar[frozenset[Capability]]` (for example
`{Capability.READ_ASSETS, Capability.READ_WORK_ORDERS, Capability.CREATE_WORK_ORDER}`).
The `ConnectorRegistry` finds connectors by capability, so the agent offers
only the LLM tools its connectors can serve, workflow steps reach the
connector that declares the action, and the [MCP server](mcp-server.md)
registers only the matching tools.

Available connectors, by configuration `type`:

| Kind | Connectors |
|------|------------|
| CMMS | `GenericCmmsConnector` (local JSON files or a REST API), `SapPmConnector`, `MaximoConnector`, `UpKeepConnector` |
| Tabular data | `ExcelCsvConnector` (Excel and CSV files), `GenericSqlConnector` (SQL databases over ODBC) |
| IoT | `OpcUaConnector`, `MqttConnector` |
| Documents | `DocumentStoreConnector` (manuals, with keyword search or a ChromaDB vector store; hybrid BM25 retrieval and reranking optional) |
| Communication | `TelegramConnector`, `SlackConnector`, `EmailConnector`, `CalendarConnector` |

`CliChannel` is a terminal channel for local runs, not a configurable
connector. The [capability matrix](capabilities.md) lists the capabilities of
every connector type, generated from the code.

### 2. Domain Model

The backbone of the framework. Every connector normalizes into these entities,
and every agent reasons in these terms. The entities are pydantic v2 models
with validators: `Asset`, `WorkOrder`, `FailureMode`, `SparePart`, `Alarm`,
`MaintenancePlan` and `Plant`. Two optional fields carry ISO 14224 codes next
to Machina's own identifiers: `Asset.equipment_class_code` (equipment class)
and `FailureMode.iso_14224_code` (failure mode).

Domain services encode maintenance logic on top of the entities:
`FailureAnalyzer` ranks probable failure modes for symptoms or an alarm,
`WorkOrderFactory` drafts work orders with deterministic content-hash IDs, and
`MaintenanceScheduler` works out maintenance windows from plans. See
[Domain Model Reference](domain.md) for the full API.

### 3. Agent Runtime

The `Agent` class orchestrates a conversation. It owns a `Plant` (the asset
registry), a `ConnectorRegistry`, an `LLMProvider`, an `EntityResolver`, the
workflow engine and a list of communication channels. Channels are also
registered in the `ConnectorRegistry`, so capability-based dispatch (for
example a workflow `channels.send_message` step) reaches them like any other
connector. When a message comes in, the runtime:

1. **Checks for a pending confirmation.** If a write is waiting for this
   user's yes/no (the two-turn confirmation used by asynchronous channels),
   the message is read deterministically, never by the LLM: an affirmation
   executes the write, anything else cancels it.
2. **Resolves entities.** `EntityResolver` matches the text against the plant
   registry and ranks candidate assets with a confidence. A verdict decides
   whether the top match *commits*: it must be confident and unambiguous.
   When candidates tie, the agent asks which asset is meant and remembers the
   offered set for the next message.
3. **Gathers context — only when the resolution commits.** For the committed
   asset the runtime takes the *first* connector that declares
   `read_work_orders`, `read_spare_parts` and `search_documents` respectively
   and runs those three queries concurrently (`asyncio.gather`); a source that
   fails is logged and skipped. Nothing else is prefetched — alarms and sensor
   readings are not gathered — and when the resolution does not commit,
   nothing is prefetched at all.
4. **Builds the prompt**: the system prompt, the gathered context (document
   chunks carry IDs the model can cite) and the conversation history.
5. **Runs the tool loop.** The LLM is called with the tools its connectors
   enable, for up to five iterations. Read results are reused within the turn.
   Every write passes three gates: the *resolution-authority* gate (a work
   order is created only for an asset this turn resolved), the
   *human-in-the-loop* confirmation (on by default — the CLI asks y/N,
   asynchronous channels confirm on the next message), and *sandbox* mode
   (the connector's `@sandbox_aware` guard turns the write into a logged
   no-op). A write repeated within the turn is not executed twice.
6. **Finalizes the turn**: citations are parsed and checked against the
   chunks retrieved this turn, guards stop leaked tool-call text and echoed
   output from reaching the user, and the history is updated before the
   reply goes back to the channel.

### 4. Workflow Engine

Workflows are declarative step lists (`Workflow`, `Step`) for repeatable
processes such as the built-in alarm-to-work-order workflow (see the
[Workflows API](api/workflows.md)). A workflow runs
when code calls `agent.trigger_workflow(name, event)` or the LLM calls the
`execute_workflow` tool; a workflow's `trigger` is metadata — nothing
schedules or subscribes to it automatically.

The engine routes each step's `action`:

- `agent.reason` — an LLM call with the step's prompt;
- `channels.send_message` — a message through the first communication connector;
- `failure_analyzer.*`, `work_order_factory.*`, `maintenance_scheduler.*`,
  `domain.*` — a domain-service method;
- `<category>.<capability>` (for example `cmms.read_spare_parts`) — the method
  of the first connector that declares that capability.

In sandbox mode write steps return a placeholder instead of executing, LLM
steps are not called, and messages are not sent. A step is a write when it
says so (`is_write=True`) or when its action name looks like one.

### 5. LLM Abstraction

A thin wrapper over [LiteLLM](https://github.com/BerriAI/litellm). Machina does
**not** build its own LLM abstraction — LiteLLM already supports 100+ providers
(OpenAI, Anthropic, Ollama, Mistral, Cohere, …). The `LLMProvider` class adds
maintenance-aware defaults and records token usage and estimated cost for each
call, nothing more.

Switching providers is a one-line change: `llm="openai:gpt-4o"` →
`llm="ollama:llama3:8b"`. The agent runtime handles both provider response
shapes transparently (`tool_calls=None` vs `tool_calls=[]`).

### 6. Observability

Structured logging via [structlog](https://www.structlog.org/) and an
`ActionTracer` that records agent actions — connector connects and queries,
LLM calls with token counts and cost, tool calls and workflow steps — for
debugging and auditing. Log lines carry structured context such as
`connector=`, `asset_id=` and `operation=`. See [Traces](observability/traces.md).

### MCP Server

The [MCP server](mcp-server.md) is a thin protocol adapter beside the agent
runtime, not part of it: it builds the connectors from the same config and
exposes their capabilities as MCP tools, resources and prompts to an
external client that brings its own LLM.

## Data flow

A user asks *"What's the status of pump P-201?"*. Here's what happens:

```text
User message
    │
    ▼
Channel (CliChannel / TelegramConnector / …)
    │
    ▼
Agent.handle_message()
    │
    ├──► pending write for this user? → the reply confirms or cancels it
    │
    ├──► EntityResolver.resolve("... pump P-201 ...")
    │    └── Asset(id="P-201"), exact ID, confidence 1.0 → verdict commits
    │
    ├──► _gather_context() — first provider per capability, concurrently
    │    ├── read_work_orders(asset_id="P-201")
    │    ├── read_spare_parts(asset_id="P-201")
    │    └── search(text, asset_id="P-201")       (document store)
    │
    ├──► _build_messages() — system prompt + context + history
    │
    ├──► _llm_loop() — LLM with the enabled tools (≤ 5 iterations)
    │    ├── reads: search_assets, read_work_orders, check_spare_parts, …
    │    ├── writes: resolution-authority → confirmation → sandbox
    │    └── final text
    │
    ├──► _finalize_turn() — citations, output guards, history
    │
    ▼
Response → Channel → User
```

## See also

- **[Domain Model Reference](domain.md)** — Every class, field, and validator
- **[Custom Connectors](connectors/custom.md)** — How to plug in a new system
- **[MCP Server](mcp-server.md)** — Exposing connectors to Claude Desktop, Cursor, etc.
