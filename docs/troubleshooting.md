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

Every `Agent` accepts a `sandbox: bool` argument. Most examples default to `sandbox=True`; the quickstart, a read-mostly Q&A agent, defaults to live.

| Sandbox on | Sandbox off (`--live`) |
|---|---|
| Read actions (lookups, queries) run normally | Same |
| Write actions (create work order, submit to CMMS) are logged only | Write actions execute for real |
| `Agent._tool_create_work_order` returns a `{"sandbox": True, ...}` dict | Returns the created work order as a dict |
| Workflow `channels.send_message` logs the message body and returns `{"sent": False, "sandbox": True, ...}` | Sends through the first communication connector |
| `Agent.start()` does not connect channels, so no SMTP login or bot polling happens | Channels connect at start |

In sandbox mode the confirmation gate is skipped: confirming a write that will not happen would mislead.

## Connector capability discovery

Every connector declares `capabilities: ClassVar[frozenset[Capability]]` (the `Capability` StrEnum in `machina.connectors.capabilities`). The agent reads it to decide which tools to offer. If your agent says "I don't have a tool for X":

```python
from machina.connectors import ConnectorRegistry

registry = ConnectorRegistry()
registry.register("sap", sap_connector)
print(registry.find_by_capability("create_work_order"))
```

Common causes of missing capabilities:

1. No connector declares the capability. `machina describe` (or the [capability matrix](capabilities.md)) lists what each connector type provides; some capabilities are configurable-only and need the right backend or settings.
2. The capability name in the workflow step action string (`cmms.create_work_order`, `channels.send_message`) does not match the connector's declared capability. Run `pytest tests/test_example_actions.py` to catch this — it validates every action string against the installed connector set.
3. The connector is an optional extra and wasn't installed. Check `pip show machina-ai` for the relevant extra (e.g. `machina-ai[sap]` for SAP PM).

## Config loader errors

`Agent.from_config()` and `machina mcp serve` read the YAML file as UTF-8, substitute `${VAR}` placeholders from the environment, and validate the result. The errors you are likely to see:

- **`ValueError: Environment variable 'X' is not set`** — the YAML references `${X}` and the variable is missing. Export it, put it in the `.env` file your entrypoint loads, or give it a default with `${X:-default}` (used when `X` is unset or empty; keep defaults for non-secret settings, so a missing secret still fails loudly).
- **`ConnectorConfigError` naming unknown settings** — the Excel/CSV and SQL connectors refuse settings keys they do not know (the error names the keys, never their values). Other connectors raise a `TypeError` for an unexpected keyword at construction.
- **`MachinaError: Unknown connector type 'x'. Available: ...`** — from `Agent.from_config()` when the `type:` is not in the connector registry (`machina describe` lists the types). The MCP server instead logs `unknown_connector_type` and skips that connector. A type whose optional extra is not installed fails with "could not be imported" and names the cause.
- **A pydantic `ValidationError`** — a value has the wrong type or an invalid enum value.

The top-level schema accepts unknown keys, so a misspelled section name (for example `setings:` instead of `settings:`) is ignored rather than rejected — if a connector seems to run with defaults, check the spelling. Keys are snake_case throughout.

## Still stuck?

- **GitHub Issues** — [github.com/LGDiMaggio/machina/issues](https://github.com/LGDiMaggio/machina/issues)
- **Discussions** — for open-ended questions, roadmap feedback, and "is this the right approach?" threads.
