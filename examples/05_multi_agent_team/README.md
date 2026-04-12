# Multi-Agent Team -- Collaborative Diagnostics

> **Status:** Coming in v0.3

Specialized agents collaborate on complex maintenance scenarios.

## Planned Architecture

```
                    +-------------------+
                    |   Orchestrator    |
                    |   (AgentTeam)     |
                    +--------+----------+
                             |
             +---------------+---------------+
             v               v               v
     +-------------+  +-------------+  +-------------+
     | Diagnostics |  |  Inventory  |  | Scheduling  |
     |   Agent     |  |   Agent     |  |   Agent     |
     +-------------+  +-------------+  +-------------+
```

- **Diagnostics Agent** -- analyzes sensor data, correlates alarms, identifies failure modes
- **Inventory Agent** -- checks spare part availability, triggers procurement
- **Scheduling Agent** -- finds optimal maintenance windows, coordinates with production

## What It Will Look Like

```python
from machina.agent import AgentTeam, Agent

team = AgentTeam(
    agents=[diagnostics_agent, inventory_agent, scheduling_agent],
    strategy="expertise_based",
)

result = await team.handle(
    "Multiple bearing alarms on cooling system pumps P-201 and P-203"
)
```

## Current Status

The `AgentTeam` class is defined in `src/machina/agent/team.py` but not yet fully implemented. Track progress in the [v0.3 milestone](https://github.com/LGDiMaggio/machina/milestone/3).

## In the Meantime

For single-agent workflows that cover most use cases, see:

- [01_alarm_response/](../01_alarm_response/) -- Built-in alarm workflow
- [02_predictive_pipeline/](../02_predictive_pipeline/) -- 10-step autonomous pipeline
- [04_custom_workflows/](../04_custom_workflows/) -- Build your own multi-step workflows
