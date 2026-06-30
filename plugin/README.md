# Machina Agent Builder (Claude Code plugin)

A thin Claude Code plugin that packages the workflow for **building and extending Machina agents with an LLM**. It carries no capability lists of its own — every command reads Machina's **code-derived self-description spine** (`machina describe`, `docs/capabilities.md`, `docs/llms.txt`), so it never goes stale as the framework changes.

This is the "Form C" surface of Machina's self-description spine: a packaged build experience over the same core that powers the `machina describe` CLI and the `machina://v1/capabilities` MCP resource.

## Commands

- **`/build-machina-agent`** — read the spine, confirm the shape with the user, then assemble a `config.yaml` + entrypoint wiring only capabilities the spine reports as backed.
- **`/extend-machina`** — add a connector, capability, MCP tool, mapper, or workflow against the seam the spine declares, then regenerate the spine so the addition is reflected (and the CI drift gate stays green).

## Install

This plugin ships inside the Machina repository (it is not distributed via PyPI). Point Claude Code at this directory (`plugin/`) as a local plugin, or add it through your plugin marketplace of choice. The commands assume you are working inside a Machina checkout with `machina-ai` installed (`pip install -e .`) so `machina describe` is available.

## Why it stays in sync

The plugin is deliberately a *consumer* of the spine, not a copy of it. Facts about what Machina can do live in code and flow through `machina.introspect.describe()` into the generated artifacts the commands read. A capability the framework gains (or loses) shows up the next time `machina describe` runs — the plugin needs no edit.
