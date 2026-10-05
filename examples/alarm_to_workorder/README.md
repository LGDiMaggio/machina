# Alarm to Work Order -- Automation in 10 Minutes

A sensor alarm fires on pump P-201. The agent handles it end-to-end: diagnose the failure, check history and spare parts, draft a work order, notify the team, submit the work order. No human in the loop -- and no LLM either: every step is deterministic.

## Run It

```bash
cd examples/alarm_to_workorder
python agent.py                     # sandbox (safe, default)
python agent.py --live              # execute the notification and the submit
```

No API key or local model is needed: the workflow never calls the LLM.

## The Code

```python
from machina import Agent, Plant
from machina.workflows.builtins import alarm_to_workorder

agent = Agent(
    name="Alarm Response Agent",
    connectors=[cmms, docs],
    channels=[CliChannel()],
    llm="ollama:llama3",              # required by Agent, unused by this workflow
    workflows=[alarm_to_workorder],   # one line to register the workflow
    sandbox=True,
)
```

That's it. The workflow template handles the rest.

## What the Workflow Does

6 steps, all deterministic -- domain services and connector calls. Fast, predictable, testable.

```
  Step                    Action                          What it does
  ─────────────────────────────────────────────────────────────────────────────
  1. analyze_alarm        failure_analyzer.diagnose       Ranks failure modes for the alarm
  2. check_history        cmms.read_maintenance_history   Completed work orders on the asset
  3. check_spare_parts    cmms.read_spare_parts           Spare parts for the asset, with stock
  4. generate_work_order  work_order_factory.create       Drafts the work order (in memory)
  5. notify_technician    channels.send_message           Sends the summary to the team   [write]
  6. submit_work_order    cmms.create_work_order          Creates the WO in the CMMS      [write]
```

The diagnosis is written into the work order only when it is at least medium-confidence (`failure_mode_for_write`); the full ranked list always appears in the description and the notification.

## Example Output

Output of `python agent.py` on the sample data (structured log lines omitted):

```
============================================================
  Alarm Response Agent  |  Mode: SANDBOX
============================================================
  Alarm:  ALM-2026-0412-001  |  Asset: P-201
  vibration_velocity_mm_s = 7.8 (threshold: 6.0)

  Workflow: Alarm to Work Order (6 steps)
============================================================

  Result: SUCCESS (0.00s)
    [+] analyze_alarm        BEAR-WEAR-01 (medium), IMP-EROSION-01 (medium), BELT-WEAR-01 (medium)
    [+] check_history        none
    [+] check_spare_parts    SKF-6310 (stock 4), SEAL-CR32-KIT (stock 1)
    [+] generate_work_order  WO-AUTO-3B717CA0: corrective, priority medium, failure mode BEAR-WEAR-01
    [+] notify_technician    sandbox: intercepted, not executed
    [+] submit_work_order    sandbox: intercepted, not executed
```

The work-order ID is a content hash, so re-running the same alarm produces the same ID and the CMMS can deduplicate it.

## Sandbox Mode

Sandbox is on by default. The two write steps -- the notification and the CMMS submit -- are logged but not executed. The diagnosis, the lookups and the drafted work order run normally, so you see exactly what would be written.

```bash
python agent.py          # sandbox -- safe to experiment
python agent.py --live   # live -- executes writes
```

## Next Steps

- [**Starter kit**](../../templates/odl-generator-from-text/) -- free-text requests to confirmed work orders, copy-configure-run
- [**More examples**](../reference/) -- Predictive pipelines, custom workflows, YAML config, autonomous agents
