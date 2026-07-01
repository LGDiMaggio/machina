---
description: Build a Machina maintenance agent by reading the framework's live self-description spine, then assembling a config and entrypoint.
argument-hint: What the agent should do (e.g. "answer questions over a SQL CMMS and equipment manuals")
---

# Build a Machina agent

You are helping build an agent on **Machina** (`machina-ai`), an async-first Python framework for industrial-maintenance agents. Do **not** guess what the framework can do from memory — read its **self-description spine**, which is generated from code and kept in sync by a CI drift gate. Never copy capability lists into your reasoning; read them live.

## 1. Read the spine first (the source of truth)

- Run **`machina describe`** (or `machina describe --json`) to get the live map of connectors × capabilities, the config schema shape, and the extension seams. If the CLI isn't installed, read **`docs/capabilities.md`** (the same data, generated) and **`docs/llms.txt`** (the curated index + conventions).
- From the spine, determine: which **connector type** fits each data source the user needs, which **capabilities** it provides as `live` vs `configurable-only` (`cfg`), and which optional **`extra`** each connector needs (`pip install "machina-ai[<extra>]"`).
- Note the spine's **"Known gaps"**: per-connector `settings` keys (connection strings, table/field mappings, endpoints) are *not* in the spine. For those, follow the spine's link to the per-connector doc under **`docs/connectors/`** — that is where connector configuration lives.

## 2. Confirm the shape with the user

Ask only what the spine leaves open: which data sources (and their access details), which channel (CLI / Telegram / Slack / Email), which LLM provider, and whether to run in sandbox mode. Do not re-ask anything the spine already answers.

## 3. Assemble the agent

- Write a **`config.yaml`**: `plant`, the chosen `connectors` (with `type`, `primary` where relevant, and `settings` taken from the per-connector doc), `channels`, `llm`, and `sandbox`. Follow **`docs/yaml-config.md`**.
- Write a short Python entrypoint that loads the config and runs the agent (see **`docs/quickstart.md`** for the canonical hero shape).
- Only wire capabilities the spine reports as backed for the chosen connectors. If the agent needs a `cfg` capability, configure the connector so it becomes available (the per-connector doc explains how).

## 4. Verify against the spine

Before declaring done, re-run `machina describe` and confirm every capability the agent relies on is actually provided (`live`) by a configured connector. If something the user needs is missing or only `cfg`, say so and resolve it — do not silently ship an agent that calls an unbacked capability.

If the user needs something Machina does not cover, switch to the **`/extend-machina`** command.
