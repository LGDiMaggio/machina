# Action Traces

Machina records every agent action — tool calls, LLM interactions, connector
operations — as structured trace entries. Traces are the primary debugging and
auditing tool.

## Trace Format

Each trace entry is a JSON object with:

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO 8601 | When the action started |
| `action` | string | Action name (e.g., `llm_call`, `cmms.read_assets`) |
| `connector` | string | Connector involved (if any) |
| `asset_id` | string | Asset involved (if any) |
| `operation` | string | Operation type |
| `input_summary` | string | Truncated input description |
| `output_summary` | string | Truncated output description |
| `duration_ms` | float | Execution time in milliseconds |
| `success` | bool | Whether the action succeeded |
| `error` | string | Error message (if failed) |
| `conversation_id` | string | Groups entries by conversation |
| `prompt_tokens` | int | LLM prompt tokens (if applicable) |
| `completion_tokens` | int | LLM completion tokens |
| `total_tokens` | int | Total tokens |
| `usd_cost` | float | Estimated USD cost |
| `model` | string | LLM model used |

## ActionTracer API

```python
from machina.observability.tracing import ActionTracer

tracer = ActionTracer(max_entries=1000)

# Record an action using the context manager
with tracer.trace("llm_call", operation="complete") as span:
    result = await llm.complete(prompt)
    span.output_summary = result[:200]
    span.prompt_tokens = result.usage.prompt_tokens
    span.completion_tokens = result.usage.completion_tokens
    span.usd_cost = result.usage.cost

# Subscribe to trace events
tracer.subscribe(lambda entry: print(entry.action))
```

## JSONL Export

Traces are exported as JSONL (one JSON object per line, one file per conversation):

```bash
ls /var/lib/machina/traces/
# 2026-04-20_conv-abc123.jsonl
# 2026-04-20_conv-def456.jsonl
```

### Redaction

The `redacting_dump_json()` method automatically:

- **Redacts** metadata values where the key matches: `token`, `password`, `secret`,
  `api_key`, `authorization`, `credential`
- **Truncates** input/output summaries to 2000 characters

Raw LLM prompts and API keys are never written to trace files.

### Reading Traces

```bash
# Pretty-print a trace file
python -m json.tool traces/2026-04-20_conv-abc123.jsonl

# Search for specific operations
grep '"action": "cmms.create_work_order"' traces/*.jsonl

# Filter by conversation
grep '"conversation_id": "abc123"' traces/*.jsonl

# Find expensive LLM calls
python -c "
import json, sys
for line in open(sys.argv[1]):
    e = json.loads(line)
    if e.get('usd_cost', 0) > 0.05:
        print(f'{e[\"action\"]}: \${e[\"usd_cost\"]:.4f} ({e[\"total_tokens\"]} tokens)')
" traces/2026-04-20_conv-abc123.jsonl
```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `MACHINA_TRACE_DIR` | `./traces` | Directory for JSONL trace files |
| `max_entries` | 1000 | In-memory buffer size per tracer |

## Integration with Log Aggregation

Trace files are plain JSONL — ship them to any log aggregation system:

- **Filebeat / Fluentd:** Watch the trace directory for new files
- **Cloud logging:** Upload JSONL files to S3/GCS, query with Athena/BigQuery
- **SIEM:** Forward for security audit (who created which work orders, when)

## See Also

- [Cost Tracking](cost.md) — LLM cost analysis from trace data
- [Uptime & Resilience](../deployment/uptime.md) — monitoring recommendations
