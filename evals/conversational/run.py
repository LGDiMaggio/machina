"""On-demand conversational eval runner — pinned local models + cloud reference.

Runs frozen multi-turn scenarios (``evals/conversational/scenarios/``)
against pinned Ollama models and emits a per-model, per-scenario
markdown report attributing each failure to a layer BY CONSTRUCTION
(the first failed assertion's layer) — never by guessing. See
``evals/README.md`` for the full contract.

Usage (PowerShell, from the repo root)::

    $env:PYTHONPATH = "$PWD\\src;$PWD"
    python -m evals.conversational.run                      # full pinned matrix
    python -m evals.conversational.run --models llama3:8b   # one model
    python -m evals.conversational.run --dry-run            # plan only, no models
    python -m evals.conversational.run --out report.md      # write instead of print

NOT a pytest suite and never wired into CI: it needs real Ollama models
(CLAUDE.md: no real APIs in tests).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = REPO_ROOT / "examples" / "sample_data"
DEFAULT_SCENARIOS_DIR = REPO_ROOT / "evals" / "conversational" / "scenarios"

# Prefer THIS repo's sources over any editable install pointing at a
# different clone (mirrors examples/quickstart/agent.py).
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from evals.conversational.schema import (  # noqa: E402
    ASSERTION_LAYERS,
    LAYER_ORDER,
    Scenario,
    Turn,
    TurnAssertions,
    load_scenarios,
)

if TYPE_CHECKING:
    from machina import Agent

DEFAULT_MODELS: tuple[str, ...] = (
    "ollama:llama3:8b",
    "ollama:qwen2.5:3b",
    "ollama:deepseek-r1:8b",
)
"""Pinned baseline tags — keep stable across eval rounds (plan: Key Decisions)."""

CLOUD_ENV_VAR = "MACHINA_EVAL_CLOUD_MODEL"
"""Env var naming the optional cloud reference model (e.g. ``gpt-4o``)."""

_KNOWN_PROVIDERS = frozenset(
    {"ollama", "openai", "anthropic", "azure", "mistral", "gemini", "groq", "bedrock"}
)

# ---------------------------------------------------------------------------
# Malformed-output sniff (deliberately decoupled from runtime internals:
# a lightweight regex/JSON check, NOT machina's private _detect_* helpers).
# ---------------------------------------------------------------------------

_RAW_TAG_RE = re.compile(r"</?\s*(think|citations)\b", re.IGNORECASE)
# Two name spellings: a "name" key (shapes A/B), or the tool name as the
# string VALUE of a "function" key (shape C, deepseek-r1:8b baseline
# 2026-06-10) — either alongside an arguments-like key.
_TOOL_JSON_RE = re.compile(
    r"\{[^{}]*\"(?:name|function)\"\s*:\s*\"[^\"]+\""
    r"[^{}]*\"(?:arguments|parameters|args|tool_input)\"\s*:"
)


def find_malformed(text: str) -> str | None:
    """Return a short diagnosis when the text looks malformed, else ``None``.

    Checks for raw ``<think>``/``<citations>`` tags and tool-call-shaped
    JSON (inline or as the whole response).

    Args:
        text: The agent's rendered answer text.

    Returns:
        A human-readable description of the first problem found, or
        ``None`` when the text is clean.
    """
    tag = _RAW_TAG_RE.search(text)
    if tag:
        return f"raw <{tag.group(1).lower()}> tag in output"
    if _TOOL_JSON_RE.search(text):
        return "tool-call-shaped JSON in output"
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        has_args = isinstance(payload, dict) and any(
            k in payload for k in ("arguments", "parameters", "args", "tool_input")
        )
        has_name = isinstance(payload, dict) and (
            "name" in payload or isinstance(payload.get("function"), str)
        )
        if has_args and has_name:
            return "response is a bare tool-call JSON object"
    return None


# ---------------------------------------------------------------------------
# Per-turn signals and assertion evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnSignals:
    """Observable signals collected for one turn (no new runtime code).

    Attributes:
        text: ``AgentResponse.text``.
        citation_sources: Non-empty ``Citation.source`` values.
        citation_count: ``len(AgentResponse.citations)``.
        invoked_tools: ``operation`` of every traced ``tool_call`` entry
            recorded during the turn.
        is_fallback: ``AgentResponse.is_fallback``.
    """

    text: str
    citation_sources: tuple[str, ...] = ()
    citation_count: int = 0
    invoked_tools: tuple[str, ...] = ()
    is_fallback: bool = False


@dataclass(frozen=True)
class AssertionResult:
    """Outcome of a single assertion against a turn's signals."""

    name: str
    layer: str
    passed: bool
    expected: str
    actual: str


def evaluate_assertions(assertions: TurnAssertions, signals: TurnSignals) -> list[AssertionResult]:
    """Evaluate a turn's active assertions in canonical layer order.

    Args:
        assertions: The turn's assertions from the scenario file.
        signals: The observable signals collected during the turn.

    Returns:
        One :class:`AssertionResult` per active assertion, in the order
        runtime -> retrieval -> citations -> golden.
    """
    results: list[AssertionResult] = []
    low_text = signals.text.lower()
    fallback_note = " [fallback]" if signals.is_fallback else ""

    for name, value in assertions.active_assertions():
        layer = ASSERTION_LAYERS[name]
        if name == "expect_tool_invoked":
            passed = value in signals.invoked_tools
            expected = f"tool '{value}' invoked"
            actual = "invoked: " + (", ".join(signals.invoked_tools) or "(none)")
        elif name == "expect_no_malformed":
            diagnosis = find_malformed(signals.text)
            passed = diagnosis is None
            expected = "no malformed output (tool JSON / <think> / <citations>)"
            actual = (diagnosis or "clean") + fallback_note
        elif name == "expect_not_fallback":
            # True -> the turn must be a real answer (is_fallback False);
            # False -> the turn must be a runtime fallback.
            passed = signals.is_fallback is (not value)
            expected = "no runtime fallback" if value else "runtime fallback"
            actual = f"is_fallback={signals.is_fallback}"
        elif name == "expect_retrieval_source":
            passed = any(value.lower() in s.lower() for s in signals.citation_sources)
            expected = f"retrieved source contains '{value}'"
            actual = "sources: " + (", ".join(signals.citation_sources) or "(none)")
        elif name == "expect_citation":
            has_citations = signals.citation_count > 0
            passed = has_citations is value
            expected = "citations present" if value else "no citations"
            actual = f"{signals.citation_count} citation(s)" + fallback_note
        elif name == "golden_contains":
            missing = [n for n in value if n.lower() not in low_text]
            passed = not missing
            expected = "answer contains: " + ", ".join(repr(n) for n in value)
            actual = (
                "all present"
                if passed
                else "missing "
                + ", ".join(repr(n) for n in missing)
                + f" | text: {signals.text[:60]!r}{fallback_note}"
            )
        else:  # golden_excludes
            present = [n for n in value if n.lower() in low_text]
            passed = not present
            expected = "answer excludes: " + ", ".join(repr(n) for n in value)
            actual = (
                "none present"
                if passed
                else "found " + ", ".join(repr(n) for n in present) + fallback_note
            )
        results.append(
            AssertionResult(
                name=name, layer=layer, passed=passed, expected=expected, actual=actual
            )
        )
    return results


# ---------------------------------------------------------------------------
# Preflight (follows the examples/_preflight.py pattern, but returns a
# message instead of sys.exit so one missing model never kills the matrix)
# ---------------------------------------------------------------------------


def preflight_model(model: str) -> str | None:
    """Check that a model is reachable; return an actionable error or ``None``.

    Args:
        model: Normalized ``provider:model`` string.

    Returns:
        ``None`` when the model looks runnable, otherwise a clear,
        actionable message (e.g. the ``ollama pull`` command to run).
    """
    provider, _, name = model.partition(":")
    if provider == "ollama":
        if not shutil.which("ollama"):
            return "Ollama is not installed. Install it from https://ollama.com"
        try:
            proc = subprocess.run(
                ["ollama", "list"], capture_output=True, timeout=10, check=False, text=True
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return "Ollama is installed but not running. Start it with: ollama serve"
        if proc.returncode != 0:
            return "Ollama is installed but not running. Start it with: ollama serve"
        pulled = [line.split()[0] for line in proc.stdout.splitlines()[1:] if line.split()]
        for tag in pulled:
            if (
                tag == name
                or tag.startswith(name + ":")
                or (":" not in name and tag.split(":")[0] == name)
            ):
                return None
        return f"model '{name}' is not pulled. Run: ollama pull {name}"
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY is not set (required for the cloud reference model)"
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY is not set (required for the cloud reference model)"
    if ":" not in model:
        # Colon-less cloud tags (the documented MACHINA_EVAL_CLOUD_MODEL form,
        # e.g. "gpt-4o" or "claude-sonnet-4-5") carry no explicit provider;
        # infer it from the model-name prefix so a missing API key yields the
        # same actionable skip row as a missing Ollama model instead of
        # per-turn ERROR spam.
        name_lower = model.lower()
        if name_lower.startswith(("gpt", "o1", "o3", "o4")) and not os.environ.get(
            "OPENAI_API_KEY"
        ):
            return "OPENAI_API_KEY is not set (required for the cloud reference model)"
        if name_lower.startswith("claude") and not os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY is not set (required for the cloud reference model)"
    return None


def normalize_model(tag: str) -> str:
    """Normalize a CLI model tag to ``provider:model`` form.

    Only tags containing a colon whose head is not a known provider get
    the ``ollama:`` prefix (``llama3:8b`` -> ``ollama:llama3:8b``); tags
    without a colon (e.g. ``gpt-4o`` or ``llama3``) and tags already
    carrying a known provider pass through unchanged.

    Args:
        tag: Raw model tag from ``--models`` or the cloud env var.

    Returns:
        The normalized model string.
    """
    tag = tag.strip()
    head = tag.split(":", 1)[0]
    if head in _KNOWN_PROVIDERS or ":" not in tag:
        return tag
    return f"ollama:{tag}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnOutcome:
    """Report row for one (model, scenario, turn)."""

    scenario_id: str
    turn_index: int
    status: str  # "pass" | "FAIL" | "ERROR"
    layer: str = ""
    evidence: str = ""


@dataclass
class ModelReport:
    """All outcomes for one model, or a skip reason."""

    model: str
    skipped: str | None = None
    outcomes: list[TurnOutcome] = field(default_factory=list)


def _build_agent(model: str, *, with_connectors: bool) -> Agent:
    """Build the eval agent exactly like examples/quickstart/agent.py.

    Heavy imports stay function-local so ``--dry-run`` (and the CI
    schema test, which never imports this module) work without machina
    or litellm importable.

    Args:
        model: Normalized model string passed to the Agent as ``llm``.
        with_connectors: ``False`` for the paired no-connector control.

    Returns:
        A configured, not-yet-started :class:`machina.Agent`. Sandbox is
        always on; temperature stays at the Agent default (0.1) for
        run-to-run reproducibility.
    """
    from machina import Agent, Plant
    from machina.connectors.cmms import GenericCmmsConnector
    from machina.connectors.docs import DocumentStoreConnector

    connectors: list[Any] = []
    if with_connectors:
        connectors = [
            GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
            DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
        ]
    return Agent(
        name="Eval Agent",
        plant=Plant(name="Eval Plant"),
        connectors=connectors,
        llm=model,
        sandbox=True,
    )


async def _run_turn(agent: Agent, turn: Turn, *, chat_id: str) -> TurnSignals:
    """Drive one turn programmatically and collect its observable signals.

    Args:
        agent: The started agent instance.
        turn: The scripted turn.
        chat_id: Conversation identifier (history is keyed by it).

    Returns:
        The signals needed to evaluate the turn's assertions.
    """
    before = len(agent.tracer.entries)
    response = await agent.handle_message_full(turn.user, chat_id=chat_id)
    new_entries = agent.tracer.entries[before:]
    invoked = tuple(e.operation for e in new_entries if e.action == "tool_call")
    sources = tuple(c.source for c in response.citations if c.source)
    return TurnSignals(
        text=response.text,
        citation_sources=sources,
        citation_count=len(response.citations),
        invoked_tools=invoked,
        is_fallback=response.is_fallback,
    )


async def _run_scenario(model: str, scenario: Scenario) -> list[TurnOutcome]:
    """Run one scenario against one model and attribute every failure.

    Attribution is by construction: the first failed assertion's layer.
    A golden-only failure in a long scenario triggers the length check
    (truncated-history replay) and becomes ``conversation-length`` when
    the replay passes, ``unattributed`` otherwise — never guessed.

    Args:
        model: Normalized model string.
        scenario: The scenario to run.

    Returns:
        One :class:`TurnOutcome` per turn.
    """
    agent = _build_agent(model, with_connectors=scenario.connectors)
    await agent.start()
    outcomes: list[TurnOutcome] = []
    chat_id = f"eval::{scenario.id}"
    try:
        for index, turn in enumerate(scenario.turns, start=1):
            try:
                signals = await _run_turn(agent, turn, chat_id=chat_id)
            except Exception as exc:
                outcomes.append(
                    TurnOutcome(
                        scenario_id=scenario.id,
                        turn_index=index,
                        status="ERROR",
                        layer="error",
                        evidence=f"{type(exc).__name__}: {str(exc)[:120]}",
                    )
                )
                continue

            results = evaluate_assertions(turn.assertions, signals)
            failed = [r for r in results if not r.passed]
            if not failed:
                outcomes.append(
                    TurnOutcome(scenario_id=scenario.id, turn_index=index, status="pass")
                )
                continue

            first = failed[0]
            layer = first.layer
            # Length check: golden-only failure (all earlier layers green by
            # evaluation order) in a long scenario -> truncated-history replay.
            if layer == "golden" and scenario.is_long:
                try:
                    replay_signals = await _run_turn(
                        agent, turn, chat_id=f"{chat_id}::length-check::{index}"
                    )
                    replay_results = evaluate_assertions(turn.assertions, replay_signals)
                    replay_passed = all(r.passed for r in replay_results)
                    layer = "conversation-length" if replay_passed else "unattributed"
                except Exception:
                    layer = "unattributed"

            outcomes.append(
                TurnOutcome(
                    scenario_id=scenario.id,
                    turn_index=index,
                    status="FAIL",
                    layer=layer,
                    evidence=f"{first.expected[:80]} vs {first.actual[:80]}",
                )
            )
    finally:
        await agent.stop()
    return outcomes


async def _run_matrix(
    models: list[str], cloud: str, scenarios: list[Scenario]
) -> list[ModelReport]:
    """Run every scenario against every available model.

    Args:
        models: Normalized local model strings.
        cloud: Normalized cloud reference model, or ``""`` when unset.
        scenarios: Validated scenarios.

    Returns:
        One :class:`ModelReport` per model (including skipped rows).
    """
    reports: list[ModelReport] = []
    all_models = list(models)
    if cloud:
        all_models.append(cloud)
    else:
        reports.append(
            ModelReport(
                model="(cloud reference)",
                skipped=f"skipped ({CLOUD_ENV_VAR} not set)",
            )
        )

    for model in all_models:
        error = preflight_model(model)
        if error:
            reports.append(ModelReport(model=model, skipped=f"skipped ({error})"))
            continue
        report = ModelReport(model=model)
        for scenario in scenarios:
            print(f"  running {model} x {scenario.id} ...", file=sys.stderr)
            try:
                report.outcomes.extend(await _run_scenario(model, scenario))
            except Exception as exc:
                # One scenario blowing up (e.g. agent.start() failing) must
                # not abort the whole matrix: record a single scenario-level
                # ERROR row (turn 0) and move on.
                report.outcomes.append(
                    TurnOutcome(
                        scenario_id=scenario.id,
                        turn_index=0,
                        status="ERROR",
                        layer="error",
                        evidence=f"{type(exc).__name__}: {str(exc)[:120]}",
                    )
                )
        reports.append(report)
    return reports


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_REPORT_LAYERS: tuple[str, ...] = (*LAYER_ORDER, "conversation-length", "unattributed", "error")


def render_report(reports: list[ModelReport], scenarios: list[Scenario]) -> str:
    """Render the per-model, per-scenario markdown report.

    Args:
        reports: One entry per model (run or skipped).
        scenarios: The scenarios that were run.

    Returns:
        The full markdown report.
    """
    lines: list[str] = []
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append("# Machina conversational eval report")
    lines.append("")
    lines.append(f"Generated: {now}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")

    ran = [r for r in reports if r.skipped is None]
    skipped = [r for r in reports if r.skipped is not None]
    total_pass = sum(1 for r in ran for o in r.outcomes if o.status == "pass")
    total_fail = sum(1 for r in ran for o in r.outcomes if o.status != "pass")

    lines.append(f"- Models run: {', '.join(r.model for r in ran) or '(none)'}")
    for r in skipped:
        lines.append(f"- {r.model}: {r.skipped}")
    scenario_names = ", ".join(s.id for s in scenarios)
    lines.append(f"- Scenarios: {len(scenarios)} ({scenario_names})")
    lines.append(f"- Turns: {total_pass} pass / {total_fail} fail")

    layer_counts = dict.fromkeys(_REPORT_LAYERS, 0)
    for r in ran:
        for o in r.outcomes:
            if o.status != "pass" and o.layer in layer_counts:
                layer_counts[o.layer] += 1
    counts = ", ".join(f"{layer}={n}" for layer, n in layer_counts.items())
    lines.append(f"- Failures by layer: {counts}")
    lines.append(
        f"- Unattributed bucket: {layer_counts['unattributed']} "
        "(golden-only failure whose truncated-history replay also failed — "
        "no observable signal claimed it; never guessed)"
    )
    lines.append("")

    for r in reports:
        lines.append(f"## {r.model}")
        lines.append("")
        if r.skipped is not None:
            lines.append(f"_{r.skipped}_")
            lines.append("")
            continue
        lines.append("| scenario | turn | status | layer | evidence |")
        lines.append("|---|---|---|---|---|")
        for o in r.outcomes:
            evidence = o.evidence.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {o.scenario_id} | {o.turn_index} | {o.status} | {o.layer or '—'} | {evidence} |"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_plan(models: list[str], cloud: str, scenarios: list[Scenario]) -> None:
    """Print the dry-run plan: resolved models and scenarios, no model calls."""
    print("DRY RUN — no models invoked, no preflight performed")
    print()
    print("Models:")
    for m in models:
        print(f"  - {m}")
    if cloud:
        print(f"  - {cloud} (cloud reference, from {CLOUD_ENV_VAR})")
    else:
        print(f"  - (cloud reference) skipped ({CLOUD_ENV_VAR} not set)")
    print()
    print("Scenarios:")
    for s in scenarios:
        assertion_count = sum(len(t.assertions.active_assertions()) for t in s.turns)
        flags = []
        if not s.connectors:
            flags.append("no-connector control")
        if s.is_long:
            flags.append("long (length check armed)")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  - {s.id}: {len(s.turns)} turn(s), {assertion_count} assertion(s){suffix}")
    print()
    total_runs = len(models) + (1 if cloud else 0)
    print(f"Plan: {total_runs} model(s) x {len(scenarios)} scenario(s). OK.")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 — the report is the output, not a CI gate).
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.conversational.run",
        description="Run frozen conversational scenarios against pinned local models.",
    )
    parser.add_argument(
        "--models",
        default="",
        help=f"Comma-separated model tags (default: {', '.join(DEFAULT_MODELS)}). "
        "Bare tags are treated as Ollama (llama3:8b -> ollama:llama3:8b).",
    )
    parser.add_argument(
        "--scenarios",
        default=str(DEFAULT_SCENARIOS_DIR),
        help="Scenario directory, file, or glob (default: evals/conversational/scenarios/)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Write the markdown report to this file instead of stdout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load scenarios, resolve models, print the plan, and exit 0 — no model calls",
    )
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if args.models:
        models = [normalize_model(m) for m in args.models.split(",") if m.strip()]
    else:
        models = list(DEFAULT_MODELS)
    cloud = (
        normalize_model(os.environ.get(CLOUD_ENV_VAR, "")) if os.environ.get(CLOUD_ENV_VAR) else ""
    )

    if args.dry_run:
        _print_plan(models, cloud, scenarios)
        return 0

    reports = asyncio.run(_run_matrix(models, cloud, scenarios))
    report_md = render_report(reports, scenarios)
    if args.out:
        Path(args.out).write_text(report_md, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(report_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
