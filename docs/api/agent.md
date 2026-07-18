# Agent

The `Agent` is the orchestrator that combines connectors, an LLM, and the domain model into a working maintenance assistant. It receives messages from channels (CLI, Telegram, Slack, …), resolves referenced assets, gathers context from connectors, calls the LLM with domain-aware prompts, and executes tool calls.

This page is the auto-generated API reference. For tutorial-level usage see [Quickstart](../quickstart.md) and [Architecture](../architecture.md).

## `Agent`

The central runtime class. Public attributes worth knowing:

- **`sandbox`** — boolean property with a propagating setter. When mutated after construction (e.g. `agent.sandbox = False` to switch from sandbox to live), the change propagates to the internal workflow engine so write actions are no longer intercepted.
- **`tracer`** — the [`ActionTracer`](observability.md) instance recording every action.
- **`plant`** — the [`Plant`](../domain.md) registered to this agent.

::: machina.agent.runtime.Agent

## `EntityResolver`

Resolves free-text mentions in user messages (e.g. "the pump P-201", "compressore C-301") to concrete assets registered on the agent's [`Plant`](../domain.md).

Both examples above resolve on the **asset ID** (`P-201`, `C-301`), which is language-independent — the surrounding word does the work only when it matches the asset's registered `name`. There is no cross-language matching and no typo tolerance: matching is verbatim containment at every stage. To make an asset resolvable by a word other than its registered name — the local-language term, plant jargon, a nickname — put that word in `Asset.aliases`, which is searched at the same authority as the name.

When several candidates tie at the top, resolution is *ambiguous*: the runtime withholds the asset for that turn, asks which one is meant, and remembers the candidates so the next message can answer by ID, by name, or by position.

::: machina.agent.entity_resolver.EntityResolver

::: machina.agent.entity_resolver.ResolvedEntity

## `LLMProvider`

Thin wrapper around LiteLLM exposing `complete()` and `complete_with_tools()`. Constructed implicitly by `Agent(llm="provider:model")` but can also be passed in explicitly for custom providers.

::: machina.llm.provider.LLMProvider

## `Capability`

Enum identifying what a connector can do. Used by `ConnectorRegistry.find_by_capability(...)` for capability-based dispatch (the agent and workflow engine both rely on this to discover available actions at runtime).

::: machina.connectors.capabilities.Capability

## Citations

When the agent answers a question from retrieved documents it returns a structured `AgentResponse` carrying inline citations alongside the prose answer. Each `Citation` references a `chunk_id` produced by `DocumentStoreConnector.search()` so callers can audit which exact passage drove the answer.

::: machina.domain.citation.AgentResponse

::: machina.domain.citation.Citation
