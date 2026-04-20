# Alarm to Work Order -- Automation in 10 Minutes

A sensor alarm fires on pump P-201. The agent handles it end-to-end: diagnose the failure, check spare parts, create a work order, notify the team. No human in the loop.

## Run It

```bash
cd examples/alarm_to_workorder
python agent.py                     # sandbox (safe, default)
python agent.py --live              # execute writes for real
python agent.py --llm openai:gpt-4o
```

## The Code

```python
from machina import Agent, Plant
from machina.workflows.builtins import alarm_to_workorder

agent = Agent(
    name="Alarm Response Agent",
    connectors=[cmms, docs],
    channels=[CliChannel()],
    llm="ollama:llama3",
    workflows=[alarm_to_workorder],   # one line to register the workflow
    sandbox=True,
)
```

That's it. The workflow template handles the rest.

## What the Workflow Does

6 steps. Only 2 use the LLM. The rest are deterministic -- fast, predictable, testable.

```
  Step                        Type           What it does
  ─────────────────────────────────────────────────────────────
  1. analyze_alarm            [RULE-BASED]   FailureAnalyzer matches alarm to failure modes
  2. check_history            [RULE-BASED]   Recent maintenance on the same asset
  3. check_spare_parts        [RULE-BASED]   Spare part availability for the failure
  4. generate_work_order      [LLM]          Agent drafts the work order description
  5. notify_technician        [RULE-BASED]   Sends structured message to the team
  6. submit_work_order        [RULE-BASED]   Creates the WO in the CMMS
```

## Example Output

```
  Alarm Response Agent  |  Mode: SANDBOX
  ============================================================
  Alarm:  ALM-2026-0412-001  |  Asset: P-201
  vibration_velocity_mm_s = 7.8 (threshold: 6.0)

  Workflow: Alarm to Work Order (6 steps)
  ============================================================

    [+] analyze_alarm         — Bearing wear (BEAR-WEAR-01), confidence: HIGH
    [+] check_history         — Last corrective WO: 2025-11-15
    [+] check_spare_parts     — SKF 6310-2RS in stock (4 units)
    [+] generate_work_order   — [LLM] Priority HIGH, est. 4 hours
    [+] notify_technician     — [SANDBOX] Message logged
    [+] submit_work_order     — [SANDBOX] WO-2026-0412 logged

  Result: SUCCESS (2.34s)
```

## Sandbox Mode

Sandbox is on by default. Write operations (create WO, send notifications) are logged but not executed. Read operations (asset lookup, spare parts check, manual search) run normally.

```bash
python agent.py          # sandbox -- safe to experiment
python agent.py --live   # live -- executes writes
```

## Next Steps

- [**Deploy to production**](../../templates/odl-generator-from-text/) -- Clone-configure-deploy starter kit with Docker
- [**More examples**](../reference/) -- Predictive pipelines, custom workflows, YAML config, autonomous agents
