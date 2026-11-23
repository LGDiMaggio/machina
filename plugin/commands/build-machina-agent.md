---
description: Build a Machina maintenance agent by reading the framework's live self-description spine, then assembling a sandboxed config and entrypoint the user approves before anything is written.
argument-hint: What the agent should do (e.g. "answer questions over a SQL CMMS and equipment manuals")
---

# Build a Machina agent

You are helping build an agent on **Machina** (`machina-ai`), an async-first Python framework for industrial-maintenance agents. Do **not** guess what the framework can do from memory. Read its **self-description spine**, which is generated from code and kept in sync by a CI drift gate. Never copy capability or connector lists into this flow; read them live.

## 1. Read the spine first (the source of truth)

- Run **`machina describe`** (or `machina describe --json`) to get the live map of connectors × capabilities, the config schema shape and the extension seams. If the CLI isn't installed, read **`docs/capabilities.md`** (the same data, generated) and **`docs/llms.txt`** (the curated index and conventions).
- From the spine, determine which **connector type** fits each data source the user needs, which **capabilities** it provides, and which optional **extra** each connector needs (`pip install "machina-ai[<extra>]"`). The spine marks each capability `yes` (declared and backed by a live method) or `cfg` (available only under the right configuration or backend). `docs/capabilities.md` prints those two words; the text view of `machina describe` lists a `yes` capability with no marker and a `cfg` one with `(configurable)`; `--json` gives `"configurable": false` or `true`.
- Note the spine's **"Known gaps"**: per-connector `settings` keys (connection strings, table or field mappings, endpoints) are *not* in the spine, and the spine carries no per-connector doc links. For settings, find the guide under **`docs/connectors/`** that covers the connector type: one guide can cover several type aliases, and its file name can differ from the type, so search the folder for the type string. If no guide covers the type, use **`docs/yaml-config.md`** and tell the user that the type has no dedicated guide.

## 2. Confirm the shape with the user

Ask only what the spine leaves open: which data sources and where they live, which channel (the default CLI, or a messaging connector from the spine), which LLM provider, which workflows, if any, and which directory the generated files go into (never the Machina repository itself unless the user says so). Do not re-ask anything the spine already answers.

**Never ask for, accept or repeat a secret value in chat** (passwords, tokens, API keys, connection strings that embed credentials). Ask only for the *name* of the environment variable that will hold each one. If the user pastes a secret anyway, do not repeat it or write it into any file; suggest they rotate it and keep it in their environment instead.

## 3. Draft the files, preview them, then write

Draft these files for the user's project:

- **`config.yaml`**, following `docs/yaml-config.md`: `plant`, the chosen `connectors` (each with `type` and the `settings` from its connector guide; if the agent uses a CMMS, mark exactly one CMMS connector `primary: true`), `channels` (a messaging channel goes here, not under `connectors`; see `docs/yaml-config.md#channels`), `llm`, and both safety flags written out explicitly on the first generation:

  ```yaml
  sandbox: true        # writes are logged, not executed
  confirmations: true  # the agent asks the user before every live write
  ```

- **Credentials** only as `${VAR}` placeholders (see `docs/yaml-config.md#environment-variables`), never as literal values in any generated file. Use `${VAR:-default}` for non-secret settings only.
- **`.env.example`**, listing every variable name the config or the LLM provider reads, with no values. Add `.env` to the project's `.gitignore`. Tell the user how the variables reach the process: Machina does not read `.env` by itself, so they export the variables in their shell or service manager, or load `.env` with a tool they already use. A blank `VAR=` line loads as an empty string (only an unset variable fails), so each line in their `.env` is either filled in or commented out.
- **An entrypoint** (for example `agent.py`) that loads the config with `Agent.from_config("config.yaml")`, keeps the config's sandbox setting by default, leaves sandbox only on an explicit `--live` flag (which sets `agent.sandbox = False` after loading), and prints the mode it runs in (sandbox or live). Write that flag handling inline; do not import helpers from the repository's `examples/` folder, which is not part of the installed package. A `machina mcp serve` deployment has no `--live` flag: there, `sandbox` comes from the config only, which may read it from the environment through a placeholder such as `${MACHINA_SANDBOX_MODE:-true}`.
- **Workflows**, if the agent needs any. They cannot be written in YAML: define them in Python and register them in the entrypoint with `agent.register_workflow(...)`, reusing a built-in from `src/machina/workflows/builtins/` first (see `docs/yaml-config.md#when-to-use-yaml-vs-python`).

Rules for this step:

- Wire only capabilities the spine reports as backed for the chosen connectors. If the agent needs a `cfg` capability, configure the connector so it becomes available (its connector guide explains how).
- `sandbox` or `confirmations` goes to `false` only on the user's explicit instruction. Record that instruction in a comment beside the flag in `config.yaml`.
- Never generate two processes (for example an agent and an MCP server) that write to the same local data directory or spreadsheet.
- **Preview before writing.** Show the user every file in full (or, for a file that already exists, a diff against it) and write nothing until they approve. On a re-run, show a diff instead of overwriting.

## 4. Check what can be checked today

`machina describe` reads connector *classes*. It never loads a config or connects to anything, so it **cannot** confirm that a configured connector works. Check only what today's tools can show:

1. **Spine:** every capability the agent relies on is `yes` for its connector type, or `cfg` with the enabling configuration taken from the connector guide. The connector's extra shows `installed: yes` in the text view of `machina describe`, unless its guide says the mode you configured needs no extra.
2. **Config:** the file loads with `machina.config.load_config("config.yaml")` once the variables are set. Report only that it loaded, or the error text: never print the loaded config or the environment, because they hold the substituted secret values. An unset `${VAR}` fails with an error that names the variable, never a value; a blank one loads, so a passing load does not prove the credentials are filled in.
3. **Sandbox run, only with the user's go** (it reads their real systems): start the entrypoint without `--live`, confirm it reports sandbox mode (stop if it does not), and ask one read-only question per data source.

Then say plainly what is **not** verified: connectivity and credentials for any source you did not read, write permissions (with `confirmations` on, the first live write still waits for the user's yes), and whether a configured instance really provides a `cfg` capability. A config-aware pre-flight check is planned as future work. Do not claim it exists, and do not run anything that pretends to be one. Never hand over an agent that relies on a capability the spine does not report as backed.

If the user needs something Machina does not cover, switch to the **`/extend-machina`** command.
