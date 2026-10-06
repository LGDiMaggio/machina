# Custom Workflows -- Build Any Maintenance Process

Your maintenance process, encoded as a workflow with LLM-powered decision steps. The workflow DSL is your superpower.

## Run It

```bash
cd examples/reference/custom_workflows
python agent.py                     # sandbox (default)
python agent.py --live              # execute writes
python agent.py --llm openai:gpt-4o
```

## Two Complete Workflows

### 1. Spare Part Reorder

Meant for the moment stock drops below the reorder point (the demo starts it by hand for `SKF-6310`). Mixes deterministic checks with an LLM urgency assessment. Machina ships no ERP connector: in sandbox mode `place_order` returns a placeholder; run live, the workflow stops there, because `create_purchase_order` is not a connector `Capability`.

```python
from machina.workflows import Workflow, Step, Trigger, TriggerType, ErrorPolicy, GuardCondition

spare_part_reorder = Workflow(
    name="Spare Part Reorder",
    trigger=Trigger(type=TriggerType.CONDITION,
                    filter={"condition": "stock_below_reorder_point"}),
    steps=[
        Step("lookup_part",        action="cmms.read_spare_parts",
             inputs={"part_id": "{trigger.part_id}"},
             on_error=ErrorPolicy.STOP),

        Step("check_dependencies", action="cmms.read_assets",
             on_error=ErrorPolicy.SKIP),

        Step("verify_criticality", action="domain.check_asset_criticality",
             guard=GuardCondition(                          # skip if no deps
                 check=lambda ctx: bool(ctx.get("check_dependencies")),
             ),
             on_error=ErrorPolicy.SKIP),

        Step("assess_urgency",     action="agent.reason",   # <-- LLM step
             prompt="...standard or expedited procurement?...",
             on_error=ErrorPolicy.SKIP),

        Step("place_order",        action="erp.create_purchase_order",
             is_write=True,                                 # blocked in sandbox
             on_error=ErrorPolicy.STOP),

        Step("notify_warehouse",   action="channels.send_message",
             template="Reorder: {trigger.part_id}...",
             on_error=ErrorPolicy.NOTIFY),
    ],
)
```

### 2. Preventive Maintenance Scheduler

Meant for a weekly run -- its trigger records the schedule (Mondays at 6 AM), but Machina does not schedule anything: start it from cron, Task Scheduler or your own service with `agent.trigger_workflow("Preventive Maintenance Scheduler")`. Scans for due plans, LLM prioritizes, batch-creates work orders.

Caveat: no connector reports when a plan last ran, so unless your code records each plan's last execution on the scheduler, `scan_due_plans` assumes every plan ran exactly one interval ago and reports every active plan as due today. Feed it real execution dates before acting on the list:

```python
preventive_scheduling = Workflow(
    name="Preventive Maintenance Scheduler",
    trigger=Trigger(type=TriggerType.SCHEDULE,
                    filter={"cron": "0 6 * * MON"}),
    steps=[
        Step("scan_plans",          action="maintenance_scheduler.scan_due_plans",
             inputs={"horizon_days": "14"}),
        Step("prioritize_work",     action="agent.reason",   # <-- LLM step
             prompt="...rank by criticality and risk..."),
        Step("create_work_orders",  action="work_order_factory.create_batch",
             is_write=True, on_error=ErrorPolicy.RETRY, retries=3),
        Step("notify_planners",     action="channels.send_message",
             template="Plans due: {scan_plans.count}..."),
    ],
)
```

## Workflow DSL Reference

### Trigger Types

A trigger describes the event a workflow handles; it does not start the workflow. Machina watches no sensors or clocks -- a run starts when your code calls `agent.trigger_workflow(name, event)` or the agent calls its `execute_workflow` tool.

| Type | Describes |
|------|-----------|
| `ALARM` | A sensor reading breaking a threshold |
| `SCHEDULE` | A time-based run (e.g. a cron expression in `filter`) |
| `CONDITION` | A custom condition becoming true |
| `MANUAL` | An explicit request from a user or API |

### Error Policies

| Policy | Behavior |
|--------|----------|
| `STOP` | Abort the entire workflow |
| `SKIP` | Skip this step, continue to next |
| `RETRY` | Retry up to N times, then fail |
| `NOTIFY` | Log the failure, continue |

### Template Variables

Reference trigger data with `{trigger.field}` and prior step outputs with `{step_name}` or `{step_name.field}`.

### Guard Conditions

Skip a step unless a condition is met:

```python
Step("verify", action="...",
     guard=GuardCondition(
         check=lambda ctx: bool(ctx.get("prior_step")),
         description="Only run if prior_step returned data",
     ))
```

### Write Steps

Mark steps that modify external systems with `is_write=True`. Sandbox mode blocks these automatically.

## Build Your Own

Template to get started:

```python
from machina.workflows import Workflow, Step, Trigger, TriggerType, ErrorPolicy

my_workflow = Workflow(
    name="My Custom Workflow",
    trigger=Trigger(type=TriggerType.MANUAL),
    steps=[
        Step("step_1", action="cmms.read_assets",          on_error=ErrorPolicy.STOP),
        Step("step_2", action="agent.reason",             prompt="Analyze {step_1}..."),
        Step("step_3", action="channels.send_message",    template="Result: {step_2}"),
    ],
)

agent = Agent(workflows=[my_workflow], sandbox=True)
result = await agent.trigger_workflow("My Custom Workflow", {"key": "value"})
```

## Next Steps

- [alarm_to_workorder/](../../alarm_to_workorder/) -- Built-in workflow template
- [predictive_pipeline/](../predictive_pipeline/) -- Full 10-step pipeline
