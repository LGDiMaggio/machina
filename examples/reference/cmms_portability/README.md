# CMMS Portability -- Same Agent, Any Backend

Switch from SAP PM to IBM Maximo by changing one line. Your agent logic, workflows, and prompts stay identical.

## Run It

```bash
cd examples/reference/cmms_portability
python agent.py                     # GenericCmms with sample data
python agent.py --backend sap       # show SAP PM configuration
python agent.py --backend maximo    # show Maximo configuration
python agent.py --interactive       # interactive chat
```

## The Key Insight

All CMMS connectors normalize to the same domain entities. Your code is portable:

```python
# Client A: SAP PM
from machina.connectors import SapPM
cmms = SapPM(url="https://sap.company-a.com/odata/v4", auth=OAuth2(...))

# Client B: IBM Maximo
from machina.connectors import Maximo
cmms = Maximo(url="https://maximo.company-b.com/oslc", auth=ApiKeyHeaderAuth(...))

# Client C: UpKeep
from machina.connectors import UpKeep
cmms = UpKeep(url="https://api.onupkeep.com/api/v2", auth=BearerAuth(...))

# The rest is IDENTICAL across all three
agent = Agent(
    connectors=[cmms, docs],
    workflows=[alarm_to_workorder],
    llm="openai:gpt-4o",
)
agent.run()
```

Same question, same domain entities, same answer format -- regardless of the CMMS behind it.

## Supported Connectors

| Connector | System | Auth | Status |
|-----------|--------|------|--------|
| `SapPM` | SAP Plant Maintenance | OAuth2, Basic | v0.1 |
| `Maximo` | IBM Maximo | API Key, Basic, Bearer | v0.1 |
| `UpKeep` | UpKeep CMMS | Bearer (Session-Token) | v0.1 |
| `GenericCmms` | Any REST-based CMMS | Configurable | v0.1 |
| `MaintainX` | MaintainX | Bearer | v0.3 |
| `Limble` | Limble CMMS | API Key | v0.3 |

## Why This Matters

If you're a **system integrator** deploying maintenance agents across multiple clients:
- Write your agent logic once
- Configure the CMMS connector per client
- Reuse all workflows, prompts, and domain services

If you're a **developer** evaluating Machina:
- Start with `GenericCmms` and sample data (this demo)
- When ready, swap in your real CMMS connector
- Zero code changes to your agent

## Next Steps

- [quickstart/](../../quickstart/) -- Start with the basics
- [alarm_to_workorder/](../../alarm_to_workorder/) -- See a workflow in action across any CMMS
