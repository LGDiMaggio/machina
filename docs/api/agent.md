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
