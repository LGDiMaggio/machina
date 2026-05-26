# Observability

Every action the agent takes — LLM calls, tool invocations, connector queries, workflow steps — is recorded as a structured [`TraceEntry`](#traceentry) on the agent's [`ActionTracer`](#actiontracer). Traces can be inspected programmatically or exported to JSONL files via the [`JSONLExporter`](#jsonlexporter).

For narrative coverage of trace shape, cost tracking, and operational use see [Action Traces](../observability/traces.md) and [Cost Tracking](../observability/cost.md). For the parallel mechanism that protects LLM-visible payloads from filesystem leaks see [Security](../deployment/security.md).

## `ActionTracer`

::: machina.observability.tracing.ActionTracer

## `TraceEntry`

::: machina.observability.tracing.TraceEntry

## `JSONLExporter`

Subscribes to an [`ActionTracer`](#actiontracer) and writes one redacted JSON object per line to a daily-rotated file. Secrets are stripped automatically — see the `_REDACT_PATTERNS` constant in the source for the exact key set.

::: machina.observability.export.jsonl.JSONLExporter
