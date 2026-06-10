# Malformed-output corpus (U8 — R11/R12)

Deterministic regression cases for every **observed malformed-output failure mode** of the
agent loop. Each case is one JSON fixture in this directory; a single parametrized test
(`tests/unit/agent/test_malformed_output_corpus.py`) discovers `corpus/*.json`, replays the
scripted completions through a fake provider against a **real `Agent`** (full
`handle_message_full` turns — loop seam + `_finalize_turn` gate, end to end), and asserts the
expected disposition against the returned `AgentResponse` fields (`text`, `is_fallback`,
`completeness`) — never via log capture.

## The process rule (R12)

> **A newly discovered malformed-output failure mode lands as a fixture in this directory
> BEFORE or WITH its fix.**

Adding a case requires **only a new fixture file** — no code edit. If the fix is deferred, the
fixture still lands immediately and pins the *current* (honest) behavior, with the gap named in
its `description`; flipping the fixture's `expected` block is then part of the future fix.
Pin reality, never aspiration.

## Fixture schema

```json
{
  "id": "<string — MUST equal the filename stem>",
  "description": "<string — names the failure mode AND its provenance (PR/transcript/doc)>",
  "user_messages": ["<optional list of user messages; one agent turn each; defaults to a single generic probe>"],
  "turns": [
    {
      "content": "<completion text, or null/empty>",
      "tool_calls": [
        {"name": "<tool name>", "arguments": "<JSON-ENCODED STRING (the provider wire format)>"}
      ]
    }
  ],
  "expected": {
    "disposition": "<see table below — required>",
    "user_text_contains": ["<optional substrings the final user-facing text must contain>"],
    "user_text_excludes": ["<optional substrings the final user-facing text must NOT contain>"],
    "is_fallback": false,
    "completeness": "complete"
  }
}
```

- Unknown top-level keys, unknown `expected` keys, unknown `turns` keys, an unknown
  `disposition`, or `id != filename stem` make the loader **fail loudly** (schema guard —
  corpus rot is caught, never skipped).
- `tool_calls[*].arguments` is a **JSON-encoded string**, not an object — mirroring the real
  provider contract (`tc.function.arguments` is a string the runtime `json.loads`).
- Fixtures are ASCII-safe, 2-space indented.

### How `turns` map to provider calls

The scripted fake consumes turns **strictly in order, one per provider call**, whichever
method the runtime invokes:

- `complete_with_tools(...)` — called by `_llm_loop` on every iteration while tools are
  offered. Consumes the next turn and returns `{"content": ..., "tool_calls": [...]}` (the
  fixture's `tool_calls` become duck-typed objects with `.function.name`,
  `.function.arguments`, `.id`).
- `complete(...)` — called only for the **forced-final** completion (no-progress break,
  duplicate-suppression limit, or iteration exhaustion). Consumes the next turn, which must be
  text-only (a `complete()` turn with `tool_calls` is a fixture error).

So a fixture exercising the force-finalization path simply places the forced completion
**last** (see `force-final-leak.json`: 5 novel tool-call turns exhaust `max_iterations`, the
6th turn is what the forced `complete()` returns). The test fails loudly if the runtime
requests more completions than scripted, or finishes with turns left over.

With multiple `user_messages`, turns are consumed across the agent turns in sequence, and
`expected` is asserted against the **final** turn's `AgentResponse` (see
`prior-turn-echo.json`).

### Dispositions

| disposition           | asserts on the final `AgentResponse`                                          |
|-----------------------|--------------------------------------------------------------------------------|
| `clean`               | `is_fallback is False` (text checks via contains/excludes)                      |
| `recovered_read`      | `is_fallback is False`; the leaked READ was recovered and the real answer shown |
| `fallback_leak`       | `text == _TOOL_CALL_LEAK_FALLBACK`, `is_fallback is True`                       |
| `fallback_leak_write` | as `fallback_leak`, plus an explicit spy assertion: the write never executed    |
| `fallback_empty`      | `text == _EMPTY_RESPONSE_FALLBACK`, `is_fallback is True`                       |
| `fallback_echo`       | `text == _REPEATED_RESPONSE_FALLBACK`, `is_fallback is True`                    |

**Universal invariant** (every case, regardless of disposition): the harness wires a spy
`create_work_order` connector and asserts **zero writes executed** — no corpus fixture may
ever legitimately mutate anything. The harness also wires a READ_ASSETS reader and a
GET_WORK_ORDER reader, so both always-on and capability-derived read tools sit on the
replayed agent's tool surface.

## Behavior notes pinned by this corpus

- **Leak disposition is per-agent (2026-06-10 eval-baseline fix).** "Known" means on THIS
  agent instance's tool surface (`_known_tool_names()`, derived from the same
  `_get_available_tools()` source dispatch uses), never the static `BUILTIN_TOOLS` list — the
  static set misclassified capability-derived tools as hallucinated and degraded recoverable
  reads to the leak fallback (`tool_call_leak_suppressed known=False tool=get_work_order` in
  the eval logs). A leaked capability-derived READ (`get_work_order`, enabled by
  `Capability.GET_WORK_ORDER`) recovers via the bounded re-entry path
  (`leaked-capability-read-recovered`); a builtin name whose enabling connector is absent is
  off-surface and suppressed exactly like a hallucinated name
  (`leaked-offsurface-builtin-suppressed`); a truly hallucinated name stays suppressed
  (`hallucinated-tool-suppressed`).

- **Loop-seam leak suppressions set `is_fallback: true` (resolved inconsistency).** When
  `_handle_text_only_completion` suppresses a leak (known write, hallucinated tool, or the
  tool-shaped JSON answer), it returns `_TOOL_CALL_LEAK_FALLBACK` as the loop's *text*;
  `_finalize_turn` recognises that sentinel and sets the structured
  `AgentResponse.is_fallback` flag, so callers can distinguish a leak fallback from a real
  answer without string-matching. This corpus originally pinned the flag as `false` on the
  three loop-seam cases (the gate saw the substituted text as ordinary prose); the fix landed
  per the R12 rule and `leaked-known-write-suppressed`, `hallucinated-tool-suppressed`,
  `legit-json-answer-pin`, and `force-final-leak` now all pin `true`.
- **KNOWN GAP — `tool-result-echo.json` is disposition `clean`.** A model whose final text is
  a verbatim dump of the raw tool-result JSON it received (the "raw-context echo" mode from
  `docs/solutions/architecture-patterns/weak-model-runtime-robustness-2026-06-07.md`) is caught
  by **no guard today**: the dump is not tool-call-shaped, not empty, and not a prior-turn
  echo, so it reaches the user raw. The fixture pins this gap for visibility; a future guard
  flips its `expected` block per the R12 rule.
- **`force-final-leak.json` has `completeness: "complete"`.** The leak fallback short-circuits
  the partial-completeness hedge — `_finalize_turn` hedges only non-fallback answers — so even
  though the loop was force-finalized, the structured field stays `"complete"` on the fallback.
  This mirrors `TestSoleEgressGate::test_forced_final_leaked_tool_call_is_suppressed` (which
  asserts only `is_fallback`).
- **`legit-json-answer-pin.json` is the accepted fail-closed trade-off (R9/U6).** A deliberate
  JSON *answer* that matches the tool-call shape is suppressed to the fallback: shape-based
  detection cannot distinguish a quoted example from a real leak, and the fallback is strictly
  safer than ever showing a leaked call raw.
- The cross-turn echo guard arms only at `>= 200` rendered characters
  (`_MIN_ECHO_LENGTH`) — `prior-turn-echo.json` uses a long canned paragraph for that reason.
- **PR #55 detector-gap families (fenced / array / wrapper / single-quoted / truncated).**
  `_detect_leaked_tool_call` normalizes before shape matching: it strips one surrounding
  markdown code fence (closing fence optional), unwraps the `{"tool_calls": [...]}` provider
  frame, resolves a top-level JSON array to its FIRST call (a known-read recovery stays
  bounded by `seen_call_keys`; trailing calls are dropped, never executed), and parses
  single-quoted pseudo-JSON via a safe Python-literal fallback (`ast.literal_eval`).
  Truncated/partial payloads never parse, so the finalize-only tripwire
  (`_looks_like_leaked_tool_call_fragment`) suppresses an unparsable payload bearing both a
  name key and a call-marker key (`arguments`/`parameters`/`tool_calls`/`function`) at the
  sole egress gate — fail-closed per the R9/U6 trade-off. Pinned by `fenced-tool-call`,
  `array-tool-calls`, `tool-calls-wrapper`, `single-quoted-tool-call`,
  `truncated-tool-call`.
- **Gap family 6 — string-valued `function` key (shape C).** The deepseek-r1:8b conversational
  eval baseline (2026-06-10, long-conversation turns 3/4/8/9/10) leaked
  `{"function": "get_asset_details", "arguments": {...}}` as user-facing answer text: the tool
  name is the string VALUE of the `function` key, so neither shape A (nested function object)
  nor shape B (top-level `name` key) matched. `_detect_leaked_tool_call` now recognises shape C
  (`function` is a non-empty string alongside `arguments`/`parameters`), the fragment tripwire
  accepts the string-valued `function` key as its name marker (truncated shape C), and the eval
  sniff (`evals/conversational/run.py`) observes the same family. Pinned by
  `function-string-key-tool-call` (landed first as disposition `clean` per R12, flipped to
  `recovered_read` with the fix — `get_asset_details` is a known READ).
