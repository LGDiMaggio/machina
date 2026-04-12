# Predictive Maintenance Pipeline

Autonomous end-to-end pipeline: sensor alarm triggers diagnosis, work order creation, scheduling optimization, and team notification. No human in the loop.

**This is the kind of agent that replaces a manual 3-hour process.**

## Run It

```bash
cd examples/02_predictive_pipeline
python agent.py
python agent.py --sandbox           # log-only mode
python agent.py --llm ollama:llama3
```

## Architecture

```
Sensor alarm (OPC-UA / MQTT / simulated)
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
|  | submit_wo         | --> WorkOrderFactory --> CMMS         |
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
  Technician receives structured alert with diagnosis,
  work order ID, spare parts, and scheduled window
```

**\* = LLM step** (3 out of 10). The other 7 are deterministic -- fast, predictable, testable without an LLM.

## The Workflow Definition

The pipeline is defined declaratively. Each step specifies what to do, not how:

```python
from machina.workflows import Workflow, Step

predictive_maintenance = Workflow(
    name="Predictive Maintenance Pipeline",
    trigger="alarm",
    steps=[
        # Phase 1: Detection
        Step("enrich_alarm", action="sensors.get_related_readings"),

        # Phase 2: Diagnosis
        Step("diagnose_rules", action="failure_analyzer.diagnose"),
        Step("search_manuals", action="docs.search"),
        Step("diagnose_llm",  action="agent.reason",
             prompt="...synthesize {diagnose_rules} + {search_manuals}..."),

        # Phase 3: Action
        Step("check_parts",   action="cmms.check_spare_parts"),
        Step("check_history", action="cmms.get_asset_history"),
        Step("draft_wo",      action="agent.reason",
             prompt="...create WO from {diagnose_llm} + {check_parts}..."),
        Step("submit_wo",     action="work_order_factory.create"),

        # Phase 4: Optimization
        Step("find_window",       action="maintenance_scheduler.find_window"),
        Step("optimize_schedule", action="agent.reason",
             prompt="...optimize {submit_wo} into {find_window}..."),
    ],
)
```

Steps reference each other with `{step_name}` template variables. The workflow engine handles context propagation automatically.

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

The workflow definition stays exactly the same. That's the power of Machina's domain model abstraction.

## Next Steps

- [01_alarm_response/](../01_alarm_response/) -- Simpler 7-step built-in workflow
- [04_custom_workflows/](../04_custom_workflows/) -- Build your own workflows from scratch
