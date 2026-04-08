# Machina

**The open-source Python framework for building AI agents specialized in industrial maintenance.**

> Machina is to industrial maintenance what LangChain is to general-purpose LLM applications.
> Build AI agents that talk to your CMMS, read your equipment manuals, monitor your sensors,
> and help your technicians — all in a few lines of Python.

## Install

```bash
pip install machina-ai
```

Optional extras for specific integrations:

```bash
pip install machina-ai[litellm]      # OpenAI / Anthropic / Ollama / 100+ providers via LiteLLM
pip install machina-ai[cmms-rest]    # GenericCmmsConnector REST mode (httpx)
pip install machina-ai[docs-rag]     # DocumentStoreConnector with ChromaDB vector search
pip install machina-ai[telegram]     # TelegramConnector for technician chat
pip install machina-ai[all]          # Everything
```

## Where to start

<div class="grid cards" markdown>

- :material-rocket-launch: **[Quickstart](quickstart.md)**

    Go from `pip install` to a working Maintenance Knowledge Agent in 30 minutes,
    using sample CMMS data and manuals — no external accounts required.

- :material-book-open-page-variant: **[Domain Model Reference](domain.md)**

    Complete API reference for `Asset`, `WorkOrder`, `FailureMode`, `SparePart`, `Alarm`,
    `MaintenancePlan`, and `Plant`. Aligned with ISO 14224.

- :material-sitemap: **[Architecture](architecture.md)**

    The five-layer architecture: connectors, domain model, agent runtime,
    LLM abstraction, and observability. How data flows end-to-end.

- :material-puzzle: **[Custom Connectors](connectors/custom.md)**

    Build your own connector by implementing a simple `BaseConnector` Protocol.
    No ABCs, no decorators — just a Python class that exposes `capabilities`.

</div>

## Why Machina?

Building an AI maintenance agent today means writing custom connectors for SAP PM,
IBM Maximo, or whatever CMMS your plant uses. It means defining domain concepts like
assets, work orders, and failure modes from scratch. It means handling OPC-UA
subscriptions, Modbus registers, and MQTT topics. And it means engineering prompts
that understand maintenance — all before writing a single line of business logic.

**That takes months. Machina makes it take minutes.** Pre-built connectors for
industrial systems, a rich domain model aligned with ISO 14224, and maintenance-aware
AI — so you can go from `pip install` to a working agent in under 30 minutes.

## License

Machina is licensed under [Apache 2.0](https://github.com/LGDiMaggio/machina/blob/main/LICENSE).
Contributions welcome — see [CONTRIBUTING.md](https://github.com/LGDiMaggio/machina/blob/main/CONTRIBUTING.md).
