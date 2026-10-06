# Cost Tracking

Every LLM call made through an agent leaves a usage record in `agent.tracer`
with its token counts and estimated USD cost.

## How It Works

After each completion, `LLMProvider` records an entry with
`action="llm_call"` and `operation="complete"` carrying:

- `model` — the LiteLLM model identifier (e.g. `openai/gpt-4o`)
- `prompt_tokens` — input tokens sent to the model
- `completion_tokens` — output tokens generated
- `total_tokens` — the sum of the two
- `usd_cost` — estimated cost from LiteLLM's pricing table

The agent passes its tracer to the provider, so this happens for the
conversation loop and for workflow `agent.reason` steps alike. Models missing
from LiteLLM's pricing table — local Ollama models, for example — get
`usd_cost = 0.0`, and a `cost_estimation_failed` warning is logged once per
model. Usage records carry no timing; the agent's separate `llm_call` span
(`operation="complete_with_tools"`) has the duration.

## Analyzing Costs

Live, from the agent:

```python
usage = [e for e in agent.tracer.entries if e.action == "llm_call" and e.operation == "complete"]
print(f"{len(usage)} calls, {sum(e.total_tokens for e in usage)} tokens, "
      f"${sum(e.usd_cost for e in usage):.4f}")
```

From [exported JSONL files](traces.md#jsonl-export) — one file per UTC day:

```bash
python -c "
import json, sys
total = 0.0
for line in open(sys.argv[1]):
    e = json.loads(line)
    if e['action'] == 'llm_call' and e['usd_cost']:
        total += e['usd_cost']
        print(f\"  {e['timestamp']}  {e['model']}  {e['total_tokens']} tokens  \${e['usd_cost']:.4f}\")
print(f'Daily total: \${total:.4f}')
" traces/traces-2026-10-02.jsonl
```

Flag days over a budget:

```bash
python -c "
import json, glob, sys
budget = float(sys.argv[1])
for f in sorted(glob.glob('traces/traces-*.jsonl')):
    total = sum(json.loads(l)['usd_cost'] for l in open(f))
    if total > budget:
        print(f'OVER BUDGET: {f}: \${total:.4f}')
" 5.00
```

The agent does not fill `conversation_id` in v0.4, so per-conversation totals
need your own grouping — for example a subscriber that tags entries as they
arrive.

## Cost Benchmarks

Rough orders of magnitude at GPT-4o pricing, for illustration only — measure
your own workload with the records above:

| Operation | Tokens | Est. Cost |
|-----------|--------|-----------|
| Simple asset lookup + response | 500–1,500 | $0.01–$0.03 |
| Failure diagnosis with manual search | 2,000–5,000 | $0.03–$0.08 |
| Work order creation with context | 1,500–3,000 | $0.02–$0.05 |

The built-in alarm-to-work-order workflow makes no LLM call, so it costs
nothing in tokens. Costs vary widely by model: local models (Ollama) cost
nothing per token, and smaller hosted models (GPT-4o-mini, Haiku) can cut costs
5–10x for simpler tasks.

## Cost Optimization

1. **Use cheaper models for simple tasks.** Asset lookups and spare part checks
   don't need GPT-4o — GPT-4o-mini or Haiku work fine.
2. **Reduce context size.** The agent prefetches the resolved asset's work
   orders, spare parts and manual excerpts into the prompt; trim large work
   order histories or document sets to relevant subsets.
3. **Cache common queries.** If technicians ask the same questions repeatedly,
   consider caching LLM responses (not built into Machina — implement at the
   application layer).
4. **Monitor with budgets.** Use the analysis above to set alerts and catch
   runaway usage.

## See Also

- [Action Traces](traces.md) — full trace format and export
- [Scaling](../deployment/scaling.md) — sizing a deployment
