# Machina Agent Builder (Claude Code plugin)

A thin Claude Code plugin that packages the workflow for **building and extending Machina agents with an LLM**. It carries no capability lists of its own — every command reads Machina's **code-derived self-description spine** (`machina describe`, `docs/capabilities.md`, `docs/llms.txt`), so it never goes stale as the framework changes.

This is the "Form C" surface of Machina's self-description spine: a packaged build experience over the same core that powers the `machina describe` CLI and the `machina://v1/capabilities` MCP resource.

## Commands

- **`/build-machina-agent`** — read the spine, confirm the shape with the user, then assemble a `config.yaml` + entrypoint wiring only capabilities the spine reports as backed.
- **`/extend-machina`** — add a connector, capability, MCP tool, mapper, or workflow against the seam the spine declares, then regenerate the spine so the addition is reflected (and the CI drift gate stays green).

## Install

The plugin ships inside the Machina repository (not on PyPI). The repo doubles as a Claude Code **plugin marketplace** — it carries a `.claude-plugin/marketplace.json` at its root that registers this plugin — so installing it is two slash commands in Claude Code.

**Prerequisite** — the commands run `machina describe`, so `machina-ai` must be installed in the environment where you use the plugin:

```shell
pip install machina-ai          # or, from a cloned checkout: pip install -e .
```

Run the plugin's commands from inside a Machina checkout, where the generated `docs/capabilities.md` / `docs/llms.txt` the commands read also live.

### Via marketplace (recommended)

Point Claude Code at the GitHub repo, then install the plugin from it:

```text
/plugin marketplace add LGDiMaggio/machina
/plugin install machina-agent-builder@machina
```

Here `machina` is the **marketplace** name (the `name` field in `.claude-plugin/marketplace.json`) and `machina-agent-builder` is the **plugin** name — hence `plugin@marketplace`.

### From a local checkout (offline / development)

Add your local clone as the marketplace instead of GitHub, then install the same way:

```text
/plugin marketplace add /absolute/path/to/machina    # the repo root you cloned
/plugin install machina-agent-builder@machina
```

Run `/plugin` at any time to open the interactive UI and browse, enable/disable, update, or remove the plugin and its marketplace.

## Why it stays in sync

The plugin is deliberately a *consumer* of the spine, not a copy of it. Facts about what Machina can do live in code and flow through `machina.introspect.describe()` into the generated artifacts the commands read. A capability the framework gains (or loses) shows up the next time `machina describe` runs — the plugin needs no edit.
