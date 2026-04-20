# Cost Tracking

Machina tracks LLM costs per action via the `ActionTracer`. Every LLM call
records token counts and estimated USD cost.

## How It Works

When the agent makes an LLM call, the tracer records:

- `prompt_tokens` — input tokens sent to the model
- `completion_tokens` — output tokens generated
- `total_tokens` — sum of prompt + completion
- `usd_cost` — estimated cost based on the model's pricing
- `model` — which model was used (e.g., `openai/gpt-4o`)

These fields appear in every trace entry where an LLM call occurs.

## Analyzing Costs

### Per-Conversation Cost

```bash
python -c "
import json, sys
total = 0
for line in open(sys.argv[1]):
    e = json.loads(line)
    cost = e.get('usd_cost', 0)
    if cost:
        total += cost
        print(f'  {e[\"action\"]}: \${cost:.4f} ({e[\"total_tokens\"]} tokens, {e[\"model\"]})')
print(f'\nTotal: \${total:.4f}')
" traces/2026-04-20_conv-abc123.jsonl
```

### Daily Cost Summary

```bash
python -c "
import json, glob
total = 0
for f in sorted(glob.glob('traces/2026-04-20_*.jsonl')):
    for line in open(f):
        total += json.loads(line).get('usd_cost', 0)
print(f'Daily total: \${total:.4f}')
"
```

### Cost Anomaly Detection

Flag conversations that exceed a budget threshold:

```bash
python -c "
import json, glob, sys
BUDGET = float(sys.argv[1])  # e.g., 0.50
for f in sorted(glob.glob('traces/*.jsonl')):
    total = sum(json.loads(l).get('usd_cost', 0) for l in open(f))
    if total > BUDGET:
        print(f'OVER BUDGET: {f} — \${total:.4f}')
" 0.50
```

## Cost Benchmarks

Typical costs for common maintenance operations (GPT-4o pricing):

| Operation | Tokens | Est. Cost |
|-----------|--------|-----------|
| Simple asset lookup + response | 500–1,500 | $0.01–$0.03 |
| Failure diagnosis with manual search | 2,000–5,000 | $0.03–$0.08 |
| Work order creation with context | 1,500–3,000 | $0.02–$0.05 |
| Full alarm-to-WO workflow (6 steps) | 5,000–15,000 | $0.08–$0.25 |

Costs vary significantly by model. Ollama (local) is free. Claude and GPT-4o
have similar per-token pricing. Smaller models (GPT-4o-mini, Haiku) can reduce
costs 5–10x for simpler tasks.

## Cost Optimization

1. **Use cheaper models for simple tasks.** Asset lookups and spare part checks
   don't need GPT-4o — GPT-4o-mini or Haiku work fine.
2. **Reduce context size.** The entity resolver and context injection functions
   control how much data is sent to the LLM. Trim large asset registries or
   maintenance histories to relevant subsets.
3. **Cache common queries.** If technicians ask the same questions repeatedly,
   consider caching LLM responses (not built into Machina — implement at the
   application layer).
4. **Monitor with budgets.** Use the per-conversation cost analysis above to
   set alerts and catch runaway conversations.

## See Also

- [Action Traces](traces.md) — full trace format and export
- [Scaling](../deployment/scaling.md) — cost-per-conversation as a scaling metric
