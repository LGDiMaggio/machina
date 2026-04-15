# Troubleshooting

A short list of the issues adopters hit most often. If your problem isn't here, open an issue — real reports become entries in this page.

## LLM provider model strings

Machina accepts both `provider:model` and `provider/model` forms on input (e.g. `"openai:gpt-4o"` or `"openai/gpt-4o"`) and normalizes to the slash form because that is what LiteLLM itself requires under the hood.

```python
from machina.llm.provider import LLMProvider

provider = LLMProvider(model="openai:gpt-4o")
assert provider.model == "openai/gpt-4o"   # normalized
```

If you see an error like `LLM Provider NOT provided. Pass in the LLM provider you are trying to call.` from LiteLLM, a model string is reaching LiteLLM without being normalized. Check whether:

1. You're calling LiteLLM directly (bypassing `LLMProvider`) — route through `LLMProvider.complete` / `complete_with_tools` instead.
2. You're passing a versioned model string with multiple colons (e.g. `openai:gpt-4o:2024-11-20`). Only the first colon is rewritten; use `openai/gpt-4o:2024-11-20` explicitly.

The `tests/unit/test_llm_provider.py::TestLiteLLMModelStringContract` class anchors this behaviour against the real LiteLLM parser — if it ever starts accepting the colon form, that test will start failing and the normalization becomes optional.

## Sandbox mode vs live mode

Every `Agent` accepts a `sandbox: bool` argument and most examples default to `sandbox=True`.

| Sandbox on | Sandbox off (`--live`) |
|---|---|
| Read actions (lookups, queries) run normally | Same |
| Write actions (create work order, submit to CMMS) are logged only | Write actions execute for real |
| `Agent._tool_create_work_order` returns a `{"sandbox": True, ...}` dict | Returns the created `WorkOrder` |
| Workflow `channels.send_message` logs the message body and returns `{"sent": False, "sandbox": True, ...}` | Attempts to actually send |

Two known gaps to be aware of:

- `Agent.start()` currently calls `connect()` on every configured channel unconditionally, regardless of `sandbox`. Connectors that talk to real services at connect-time (e.g. `EmailConnector` doing SMTP login) will attempt a live login even in sandbox mode. Tracked in [#31](https://github.com/LGDiMaggio/machina/issues/31) for v0.3.
- The workflow notification step resolves communication connectors via the registry, not the `channels` list (also [#31](https://github.com/LGDiMaggio/machina/issues/31)). See the `examples/01_alarm_response/README.md` for the implication.

## Connector capability discovery

Every `BaseConnector` declares a `capabilities: ClassVar[list[str]]` constant. The agent reads this at startup to decide which tools are available. If your agent says "I don't have a tool for X":

```python
from machina.connectors import ConnectorRegistry

registry = ConnectorRegistry()
registry.register("sap", sap_connector)
print(registry.find_by_capability("create_work_order"))
```

Common causes of missing capabilities:

1. The connector is attached via `channels=[...]` instead of `connectors=[...]`. Communication connectors exposed as channels are not in the registry (see the sandbox section above).
2. The capability name in the workflow step action string (`cmms.create_work_order`, `channels.send_message`) does not match the connector's declared capability. Run `pytest tests/test_example_actions.py` to catch this — it validates every action string against the installed connector set.
3. The connector is an optional extra and wasn't installed. Check `pip show machina-ai` for the relevant extra (e.g. `machina-ai[sap]` for SAP PM).

## Config loader errors

Machina uses YAML + env var + Python config with a fixed precedence (env vars override YAML override defaults). If a key is missing or mistyped, you'll get a `ConfigError` naming the key and the source file:

```
ConfigError: required key 'llm' missing in machina.yaml
```

Common causes:

- The env var name uses the wrong prefix. Machina reads `MACHINA_*` variables; a `SMTP_HOST=...` setting is ignored, you need `MACHINA_SMTP_HOST=...`.
- The YAML key and the Python key disagree. Both must be snake_case (`smtp_host`, not `smtpHost`).
- A port env var is set to a non-numeric string (e.g. trailing whitespace). `examples/01_alarm_response/agent.py` validates `MACHINA_SMTP_PORT` and raises `SystemExit` with a clear message if it isn't an integer — mirror that pattern in your own wiring.

## Still stuck?

- **GitHub Issues** — [github.com/LGDiMaggio/machina/issues](https://github.com/LGDiMaggio/machina/issues)
- **Discussions** — for open-ended questions, roadmap feedback, and "is this the right approach?" threads.
