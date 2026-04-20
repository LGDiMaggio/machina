# 07 — Agent-Driven Maintenance

An autonomous agent that decides what to do **without predefined workflows**.

## Workflow vs Agent-Driven

| Aspect | Workflow (examples 01-04) | Agent-Driven (this example) |
|--------|--------------------------|----------------------------|
| Who decides the sequence? | Developer | LLM |
| Adapts to new scenarios? | No (fixed steps) | Yes (reasons about tools) |
| Auditability | High (steps are traced) | Medium (tool calls are logged) |
| Best for | Regulated, repeatable processes | Exploratory diagnostics, ad-hoc requests |

## What Happens

The agent receives a complex scenario:

> Pump P-201 shows anomalous vibrations at 8.5 mm/s (threshold: 7.0 mm/s).
> What should we do?

The agent autonomously decides to:

1. **Search for asset P-201** (`search_assets`) -- identify the equipment
2. **Check work order history** (`read_work_orders`) -- see recent maintenance
3. **Diagnose the failure** (`diagnose_failure`) -- match symptoms to failure modes
4. **Search equipment manual** (`search_documents`) -- find the bearing replacement procedure
5. **Check spare parts** (`check_spare_parts`) -- verify bearing availability
6. **Create a predictive work order** (`create_work_order`) -- schedule the intervention

The order and selection of tools is decided by the LLM at runtime.

## Run

```bash
# Default: sandbox mode (writes are logged, not executed)
python agent.py

# With a specific LLM
python agent.py --llm openai:gpt-4o

# Live mode (actually creates work orders in the CMMS)
python agent.py --live

# Debug output
python agent.py --verbose
```

## When to Use This Pattern

Use agent-driven when:
- The situation is new or unpredictable
- You want the agent to reason about what tools to use
- You're building a conversational maintenance assistant

Use workflows (examples 01-04) when:
- The process must follow a fixed, auditable sequence
- Compliance or safety requires predetermined steps
- Performance matters (workflows skip LLM calls for deterministic steps)
