# Machina conversational evals

An on-demand eval harness that runs frozen multi-turn scenarios against
pinned local models (plus an optional cloud reference) and emits a
per-model, per-scenario markdown report. Every failure is attributed to a
layer **by construction** — never by guessing.

**Not a pytest suite. Never wired into CI** — it needs real Ollama models.
Only the scenario schema/loader is CI-tested
(`tests/unit/test_eval_scenario_schema.py`).

## How to run

From the repo root (PowerShell):

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD"

# Full pinned matrix (all default models, all scenarios)
python -m evals.conversational.run

# One model, custom output file
python -m evals.conversational.run --models llama3:8b --out report.md

# Plan only — loads scenarios, resolves models, no model calls
python -m evals.conversational.run --dry-run
```

| Flag | Meaning |
|---|---|
| `--models` | Comma-separated tags, overrides the pinned defaults. Bare tags are treated as Ollama (`llama3:8b` → `ollama:llama3:8b`). |
| `--scenarios` | Scenario directory, single file, or glob. Default: `evals/conversational/scenarios/`. |
| `--out` | Write the markdown report to a file instead of stdout. |
| `--dry-run` | Print the resolved plan and exit 0 — no preflight, no model calls. |

The exit code is always 0 on a completed run: the report is the output,
not a CI gate.

## Pinned model tags

| Model | Why it is pinned |
|---|---|
| `ollama:llama3:8b` | The quickstart default; produced the documented citation/synthesis failures. |
| `ollama:qwen2.5:3b` | The 3B class that produced the documented tool-call-contract breaks. |
| `ollama:deepseek-r1:8b` | Reasoning model — exercises `<think>` leakage. |

Keep these tags stable across eval rounds so reports stay comparable.
Pull them with `ollama pull <tag>`. The runner preflights each model
(Ollama installed, server running, tag pulled) and reports a skipped row
with an actionable message instead of crashing the matrix.

Temperature is the Agent default (0.1) and is deliberately not overridden,
for run-to-run reproducibility.

## Environment variables

| Variable | Effect |
|---|---|
| `MACHINA_EVAL_CLOUD_MODEL` | Optional cloud reference model (e.g. `gpt-4o`, `anthropic:claude-sonnet-4-20250514`). Unset → the report shows the row "skipped (MACHINA_EVAL_CLOUD_MODEL not set)" and the run continues. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Required by the corresponding cloud provider (checked in preflight). |
| `PYTHONPATH` | Must include both `src` and the repo root (see "How to run") so the runner uses THIS clone's sources and the `evals` package resolves. |

## Adding a scenario

Drop a YAML file in `evals/conversational/scenarios/`. One scenario per
file; `example_smoke.yaml` is a documented template. Shape:

```yaml
id: my-scenario            # unique across all scenario files
description: >
  What this scenario measures.
connectors: false          # OPTIONAL — paired no-connector control (default: true)
turns:
  - user: "Self-contained user message (never references a prior answer)"
    assertions:            # OPTIONAL mapping — every key below is optional
      expect_tool_invoked: "list_assets"        # runtime: traced tool_call op
      expect_no_malformed: true                 # runtime: default true; set false to disable
      expect_retrieval_source: "pump_p201"      # retrieval: substring of a citation source
      expect_citation: true                     # citations: require (true) / forbid (false)
      golden_contains: ["P-201", "bearing"]     # golden: case-insensitive substrings
      golden_excludes: ["I don't know"]         # golden: must NOT appear
```

Rules enforced by the loader (and by the CI schema test on every file):

- `id`, `description`, and a non-empty `turns` list are required.
- Unknown top-level, turn, or assertion keys are rejected with the
  offending field named.
- Every `user` turn must be self-contained (R13) so per-turn assertions
  stay well-defined for every model and history length.

## The layer-attribution contract

Every assertion type maps to exactly **one** layer:

| Assertion | Layer | Observable signal |
|---|---|---|
| `expect_tool_invoked` | runtime | `agent.tracer` entries with action `tool_call` recorded during the turn |
| `expect_no_malformed` | runtime | lightweight sniff of `AgentResponse.text` (tool-call-shaped JSON, raw `<think>` / `<citations>` tags) |
| `expect_retrieval_source` | retrieval | `Citation.source` values on `AgentResponse.citations` |
| `expect_citation` | citations | `len(AgentResponse.citations)` |
| `golden_contains` / `golden_excludes` | golden | `AgentResponse.text` |

Assertions are evaluated in the fixed order
**runtime → retrieval → citations → golden**; the failing layer is the
layer of the FIRST failed assertion.

Two refinements, still by construction:

- **conversation-length**: a turn that fails ONLY the golden layer (all
  earlier signals green) in a long scenario (10+ turns) is replayed with
  truncated history (a fresh conversation containing only that turn). If
  the replay passes, the failure is attributed to `conversation-length`.
- **unattributed**: if that replay also fails, the failure is reported as
  `unattributed` — no observable signal claimed it, and the harness never
  guesses. The report calls this bucket out explicitly.

A turn that raises (e.g. the LLM call itself fails) is reported with
status `ERROR` and counted separately — it is a harness/provider error,
not an attributed model failure.

## Layout

```
evals/
├── README.md                      # this file
└── conversational/
    ├── run.py                     # async runner + report (python -m evals.conversational.run)
    ├── schema.py                  # scenario schema + loader (CI-tested, dependency-light)
    └── scenarios/                 # one YAML per scenario (suite arrives in U10)
        └── example_smoke.yaml
```
