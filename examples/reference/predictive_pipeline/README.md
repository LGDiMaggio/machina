# Predictive Maintenance Pipeline

An end-to-end pipeline in one workflow: an alarm event goes through rule-based and LLM diagnosis, a drafted work order and a proposed maintenance window.

The script opens a CLI chat with the workflow registered. Ask the agent to run it (it calls its `execute_workflow` tool, which asks for your confirmation by default), or call `agent.trigger_workflow("Predictive Maintenance Pipeline", alarm_event)` from your own alarm handler -- Machina does not subscribe to alarms for you. The sensor data comes from `SimulatedSensorConnector` over the sample sensor logs.

## Run It

```bash
cd examples/reference/predictive_pipeline
python agent.py                     # sandbox (default — writes are logged, not executed)
python agent.py --live              # execute writes for real
python agent.py --llm ollama:llama3
```

## Architecture

```
Alarm event (passed to the workflow by your code or the agent)
    |
    v
+-------------------------------------------------------------+
|                    WORKFLOW ENGINE                           |
|                                                             |
|  Phase 1: DETECTION                                         |
|  +------------------+                                       |
|  | enrich_alarm      | --> read correlated sensors           |
|  +--------+---------+                                       |
|           v                                                  |
|  Phase 2: DIAGNOSIS                                          |
|  +------------------+                                       |
|  | diagnose_rules    | --> FailureAnalyzer (deterministic)   |
|  | search_manuals    | --> DocumentStore RAG                 |
|  | diagnose_llm      | --> LLM synthesizes evidence     *   |
|  +--------+---------+                                       |
|           v                                                  |
|  Phase 3: ACTION                                             |
|  +------------------+                                       |
|  | check_parts       | --> spare part availability           |
|  | check_history     | --> maintenance history from CMMS     |
|  | draft_wo          | --> LLM writes WO description    *   |
|  | submit_wo         | --> WorkOrderFactory (drafts the WO)  |
|  +--------+---------+                                       |
|           v                                                  |
|  Phase 4: OPTIMIZATION                                       |
|  +------------------+                                       |
|  | find_window       | --> MaintenanceScheduler              |
|  | optimize_schedule | --> LLM optimizes timing         *   |
|  +------------------+                                       |
+-------------------------------------------------------------+
    |
    v
  Workflow result: diagnosis, drafted work order, spare parts,
  and a proposed maintenance window
```

**\* = LLM step** (3 out of 10). The other 7 are deterministic -- fast, predictable, testable without an LLM.

## The Workflow Definition

The pipeline is defined declaratively. Each step specifies what to do, not how:

```python
from machina.workflows import Workflow, Step

asset = {"asset_id": "{trigger.asset_id}"}

predictive_maintenance = Workflow(
    name="Predictive Maintenance Pipeline",
    trigger="alarm",
    steps=[
        # Phase 1: Detection
        Step("enrich_alarm", action="sensors.get_related_readings", inputs=asset),

        # Phase 2: Diagnosis
        Step("diagnose_rules", action="failure_analyzer.diagnose",
             inputs={**asset, "parameter": "{trigger.parameter}", "value": "{trigger.value}",
                     "severity": "{trigger.severity}"}),
        Step("search_manuals", action="docs.search_documents",
             inputs={"query": "{trigger.parameter} {trigger.asset_id}"}),
        Step("diagnose_llm",  action="agent.reason",
             prompt="...synthesize {diagnose_rules} + {search_manuals}..."),

        # Phase 3: Action
        Step("check_parts",   action="cmms.read_spare_parts", inputs=asset),
        Step("check_history", action="cmms.read_maintenance_history", inputs=asset),
        Step("draft_wo",      action="agent.reason",
             prompt="...create WO from {diagnose_llm} + {check_parts}..."),
        Step("submit_wo",     action="work_order_factory.create", is_write=False,
             inputs={**asset, "failure_mode": "{diagnose_rules.failure_mode_for_write}",
                     "description": "{draft_wo}"}),

        # Phase 4: Optimization
        Step("find_window",       action="maintenance_scheduler.find_window", inputs=asset),
        Step("optimize_schedule", action="agent.reason",
             prompt="...optimize {submit_wo} into {find_window}..."),
    ],
)
```

Connector and domain-service steps receive only the arguments their `inputs`
pass; the engine fills the `{trigger.…}` and `{step_name}` template variables
from the alarm event and the earlier steps' outputs, and `agent.reason` prompts
use the same variables. In sandbox mode the three LLM steps return a
placeholder instead of calling the model, so a sandbox run shows the wiring
and the deterministic results without an API key.

## Connecting Real Systems

Replace the sample connectors to go to production:

```python
# Instead of sample data:
from machina.connectors import SapPM, OpcUA, Telegram

agent = Agent(
    connectors=[
        SapPM(url="https://sap.yourcompany.com/odata/v4", ...),
        OpcUA(endpoint="opc.tcp://plc-line2:4840", subscriptions=[...]),
        DocumentStore(paths=["./manuals/"]),
    ],
    channels=[Telegram(bot_token="...")],
    workflows=[predictive_maintenance],  # same workflow, real data
)
```

The CMMS and document steps stay exactly the same -- that's the domain model abstraction at work. The sensor step needs a connector that declares `get_related_readings`; in v0.4 only the simulated sensor connector does (the OPC-UA and MQTT connectors expose subscriptions and node reads instead), so adapt `enrich_alarm` to your sensor source. `submit_wo` drafts the work order in memory; add a `cmms.create_work_order` step to write it to the CMMS.

## Next Steps

- [alarm_to_workorder/](../../alarm_to_workorder/) -- Simpler 6-step built-in workflow
- [custom_workflows/](../custom_workflows/) -- Build your own workflows from scratch
