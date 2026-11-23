---
description: Extend Machina at a declared seam — add a connector, capability, MCP tool, mapper, or workflow — using the spine's extension manifest.
argument-hint: What to add (e.g. "a connector for the Fiix CMMS REST API")
---

# Extend Machina at a seam

Machina is meant to be extended. The **extension seams** are declared in the self-description spine, so you never have to reverse-engineer them from source. Read them, then implement against the named seam. Extension work happens in a Machina checkout; the paths below are repository paths.

## 1. Read the seam manifest

- Run **`machina describe`** (or read the seam section of **`docs/capabilities.md`**) to get the live list of seams: the reflectable **Protocols** (such as `BaseConnector`) with their required methods, and the **convention seams** (transport/mapper split, connector-type registration, the `Capability` enum, MCP-tool registration, workflow builtins), each with its location template.
- For a new **connector**, read **`docs/connectors/custom.md`** for the full method contract and an end-to-end example.

## 2. Implement against the seam (do not invent structure)

- **Add a connector** → create `src/machina/connectors/{category}/{name}.py` and implement the `BaseConnector` Protocol **structurally** (a bare class — never subclass it). Declare `capabilities: ClassVar[frozenset[Capability]]` using members of the `Capability` StrEnum. Keep transport in the connector and vendor payload↔domain mapping in `src/machina/connectors/cmms/mappers/{vendor}.py`. All I/O is `async`; heavy transport deps are imported lazily inside methods and gated behind a pip extra.
- **Register the type** → add a `type → dotted factory path` entry to `_CONNECTOR_FACTORIES` in `src/machina/runtime.py`.
- **Add a capability** → add a member to the `Capability` StrEnum (`src/machina/connectors/capabilities.py`), then map it to its backing method in `CAPABILITY_TO_METHOD` (`src/machina/introspect/_methods.py`) — capability values are not always method names.
- **Expose it as an MCP tool** → map the capability in `CAPABILITY_TO_TOOL` (`src/machina/mcp/tools.py`).

Ground rules while you implement:

- **Preview before writing.** Show the user a diff of every file you add or change, and write nothing until they approve.
- **Never ask for, accept or repeat a secret value in chat.** Any config example you write for the addition uses `${VAR}` placeholders for credentials and `sandbox: true`; tests and fixtures carry no real credentials.

## 3. Make the spine reflect your addition

The spine is code-derived and CI-gated, so your new connector/capability must show up in it:

- Declare a capability only when a **live** backing method exists (a declared-but-unimplemented capability is worse than an undeclared one — the invariant test will fail it).
- Regenerate the artifact: **`python scripts/gen_spine.py`** and commit `docs/capabilities.{md,json}`. `make ci` (the drift gate) goes red until you do; `python scripts/gen_spine.py --check` confirms it locally.
- Run `machina describe` and confirm your addition appears as expected. It reads connector classes and never instantiates them, so it shows what a class declares, not what a configured instance provides.
