# MachinaRuntime

`MachinaRuntime` is the headless façade used by the MCP server and other non-agent entry points. It owns a `ConnectorRegistry` and exposes connector lookup by name or by capability without dragging in the LLM, channels, or workflow engine that [`Agent`](agent.md) carries.

Use `Agent` for conversational and workflow-driven scenarios. Use `MachinaRuntime` when you want to expose connector capabilities to an external orchestrator (MCP, custom server, batch script) and the LLM lives outside Machina.

::: machina.runtime.MachinaRuntime
