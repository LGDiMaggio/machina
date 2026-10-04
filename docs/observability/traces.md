# Action Traces

Every `Agent` records what it does — connector calls, LLM calls, tool calls,
workflow steps — as structured trace entries in `agent.tracer`, an
`ActionTracer`. Traces are the primary debugging and auditing tool.

## What Gets Traced

| `action` | Recorded by | `operation` | Notes |
|----------|-------------|-------------|-------|
| `connector_connect` | `Agent.start()` | — | One per connector, with timing |
| `load_assets` | `Agent.start()` | — | `output_summary`: "Loaded N assets" |
| `connector_query` | context prefetch | `read_work_orders`, `read_spare_parts`, `search_documents` | With `connector` and `asset_id` |
| `llm_call` | agent tool loop | `complete_with_tools` | Timing span for one LLM round-trip |
| `llm_call` | `LLMProvider` | `complete` | Usage record: `model`, token counts, `usd_cost` (no timing) |
| `tool_call` | agent tool loop | the tool name | `output_summary`: the first 200 characters of the result |
| `workflow_step` | workflow engine | the step name | `output_summary`: the first 200 characters of the output |

Each LLM call therefore leaves two entries: a timing span from the agent and a
usage record from the provider. Workflow LLM steps (`agent.reason`) produce
usage records too, since the engine shares the agent's tracer and provider.

## Trace Format

Each entry is a `TraceEntry` (pydantic model):

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO 8601 (UTC) | When the entry was created |
| `action` | string | Action category (see the table above) |
| `connector` | string | Connector involved, if any |
| `asset_id` | string | Asset context, if any |
| `operation` | string | Specific operation |
| `input_summary` | string | Short input description (unused by the built-in spans) |
| `output_summary` | string | Short output description |
| `duration_ms` | float | Execution time in milliseconds (0 for usage records) |
| `success` | bool | `false` when the traced block raised |
| `error` | string | The exception message, if it failed |
| `metadata` | object | Extra keyword arguments passed to `trace()` |
| `conversation_id` | string | Reserved for grouping by conversation; the agent does not fill it in v0.4 |
| `prompt_tokens` | int | LLM input tokens (usage records) |
| `completion_tokens` | int | LLM output tokens (usage records) |
| `total_tokens` | int | Sum of the two |
| `usd_cost` | float | Estimated USD cost (usage records) |
| `model` | string | LLM model identifier (usage records) |

## ActionTracer API

```python
# Inspect what the agent did
for entry in agent.tracer.entries:
    print(entry.action, entry.operation, entry.duration_ms, entry.usd_cost)

# React to every new entry
agent.tracer.subscribe(lambda entry: print(entry.action))

# Trace your own code: the context manager times the block, marks failures,
# and records the entry on exit
with agent.tracer.trace("report_export", asset_id="P-201") as span:
    rows = export_report()
    span.output_summary = f"{len(rows)} rows"
```

The tracer keeps the last 1,000 entries in memory (`ActionTracer(max_entries=...)`
for a standalone tracer).

## JSONL Export

Nothing is written to disk unless you attach an exporter:

```python
from machina.observability.export.jsonl import JSONLExporter

JSONLExporter("/var/lib/machina/traces").attach(agent.tracer)
```

The exporter appends one redacted JSON line per entry to one file per UTC day,
`traces-YYYY-MM-DD.jsonl` (`rotate_daily=False` writes a single
`traces.jsonl`). It creates the directory, and on POSIX systems restricts it
to mode `0700` and the files to `0600`. The directory is whatever path you
pass; no environment variable sets it.

### Redaction

Each line is produced by `TraceEntry.redacting_dump_json()`, which:

- **redacts** `metadata` values whose key contains `token`, `password`,
  `secret`, `api_key`, `client_secret` or `authorization` (case-insensitive);
- **truncates** `input_summary` and `output_summary` to 2,000 characters.

Prompts and message text are not traced. Tool results and workflow outputs
appear only as their first 200 characters in `output_summary`, so treat trace
files as semi-sensitive.

### Reading Traces

```bash
# Every tool call of the day
grep '"action": "tool_call"' traces/traces-2026-10-02.jsonl

# Work orders created through the agent
grep '"operation": "create_work_order"' traces/*.jsonl

# LLM calls above 5 cents
python -c "
import json, sys
for line in open(sys.argv[1]):
    e = json.loads(line)
    if e['usd_cost'] > 0.05:
        print(e['timestamp'], e['model'], e['total_tokens'], round(e['usd_cost'], 4))
" traces/traces-2026-10-02.jsonl
```

## Integration with Log Aggregation

Trace files are plain JSONL — ship them to any log aggregation system:

- **Filebeat / Fluentd:** watch the trace directory for new files
- **Cloud logging:** upload the files to S3/GCS and query with Athena/BigQuery
- **SIEM:** forward them for audit (which tool calls ran, when)

## See Also

- [Cost Tracking](cost.md) — LLM cost analysis from trace data
- [Uptime & Resilience](../deployment/uptime.md) — monitoring recommendations
