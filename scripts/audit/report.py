"""Merge the audit legs' result files into one report and verdict.

    python scripts/audit/report.py --results audit-results \\
        --expected "newest-py3.12 package-smoke security" \\
        --out audit-report --summary "$GITHUB_STEP_SUMMARY"

Reads every ``result.json`` under ``--results`` (one per downloaded leg
artifact), writes ``audit-report.json`` and ``audit-report.md`` into
``--out`` and appends the Markdown to ``--summary``. Exit code:

* 0 green: every expected leg passed.
* 1 red: a leg that counts failed.
* 2 incomplete: an expected leg left no result, a result is malformed,
  duplicated or unexpected, or a leg that counts errored or executed nothing.
  Incomplete outranks red: an unfinished audit cannot vouch for anything.

Informational legs must still leave a well-formed result, but their status
never changes the verdict.

A skipped test is never coverage. The report also lists every test or test
module that no suite leg executed, with each leg's skip reason, so a suite
that only ever skips is named instead of hidden inside a passing count.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from record import test_module

SCHEMA = "machina-audit-result/1"
CHECKS = ("suite", "package-smoke", "security")
STATUSES = ("pass", "fail", "error")
VERDICTS = {0: "green", 1: "red", 2: "incomplete"}


def problems(result: Any) -> list[str]:
    """Why ``result`` does not follow the result schema; empty when it does."""
    if not isinstance(result, dict):
        return ["not a JSON object"]
    facts, findings = result.get("facts"), result.get("findings")
    facts = facts if isinstance(facts, dict) else {}
    executed = facts.get("executed")
    skipped, by_module = facts.get("skipped_tests", []), facts.get("executed_by_module", {})
    rules = {
        f"schema is not {SCHEMA}": result.get("schema") == SCHEMA,
        f"check is not one of {CHECKS}": result.get("check") in CHECKS,
        "variant is empty": isinstance(result.get("variant"), str) and result["variant"],
        f"status is not one of {STATUSES}": result.get("status") in STATUSES,
        "informational is not a boolean": isinstance(result.get("informational"), bool),
        "facts.executed is not a count": type(executed) is int and executed >= 0,
        "findings are not {rule, message} objects": isinstance(findings, list)
        and all(isinstance(f, dict) and {"rule", "message"} <= f.keys() for f in findings),
        "facts.skipped_tests are not {test, reason} objects": isinstance(skipped, list)
        and all(isinstance(s, dict) and {"test", "reason"} <= s.keys() for s in skipped),
        "facts.executed_by_module is not a count per module": isinstance(by_module, dict)
        and all(type(count) is int for count in by_module.values()),
    }
    return [message for message, ok in rules.items() if not ok]


def not_executed(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tests and modules that every suite leg with test results skipped.

    A test ran in a leg when its module executed there and the test is not
    among that leg's skips; a module-level skip id (no ``::``) ran nowhere
    when no leg executed any test in that module.
    """
    runs = [
        leg for leg in legs if leg["check"] == "suite" and "executed_by_module" in leg["facts"]
    ]
    executed = Counter[str]()
    skips: dict[str, dict[str, str]] = {}  # test id -> {variant: reason}
    for leg in runs:
        executed.update(leg["facts"]["executed_by_module"])
        for entry in leg["facts"]["skipped_tests"]:
            skips.setdefault(entry["test"], {})[leg["variant"]] = entry["reason"]
    missing: dict[str, dict[str, set[str]]] = {}  # id -> {reason: variants}
    for test, reasons in skips.items():
        module = test_module(test)
        if executed[module] == 0:
            test = module  # the whole module ran nowhere: report it once
        elif "::" not in test or any(
            leg["facts"]["executed_by_module"].get(module, 0) and leg["variant"] not in reasons
            for leg in runs
        ):
            continue
        for variant, reason in reasons.items():
            missing.setdefault(test, {}).setdefault(reason, set()).add(variant)
    every = {leg["variant"] for leg in runs}
    return [
        {
            "test": test,
            "whole_module": "::" not in test,
            "reasons": {
                reason: "every leg" if variants == every else ", ".join(sorted(variants))
                for reason, variants in sorted(by_reason.items())
            },
        }
        for test, by_reason in sorted(missing.items())
    ]


def build(results_dir: Path, expected: list[str]) -> dict[str, Any]:
    """Validate every result against ``expected`` and decide the verdict."""
    legs: dict[str, dict[str, Any]] = {}
    incomplete = [] if expected else ["no expected legs were given"]
    red = []
    for path in sorted(results_dir.rglob("result.json")):
        where = path.relative_to(results_dir).as_posix()
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            incomplete.append(f"{where}: unreadable ({exc})")
            continue
        if errors := problems(result):
            incomplete.append(f"{where}: malformed ({'; '.join(errors)})")
            continue
        variant = result["variant"]
        if variant in legs:
            incomplete.append(f"{variant}: more than one result")
        elif variant not in expected:
            incomplete.append(f"{variant}: not an expected leg")
        legs[variant] = result
    for variant in expected:
        result = legs.get(variant)
        if result is None:
            incomplete.append(f"{variant}: no result")
        elif result["informational"]:
            continue
        elif result["status"] == "error":
            incomplete.append(f"{variant}: the check could not run")
        elif result["facts"]["executed"] == 0:
            incomplete.append(f"{variant}: executed nothing")
        elif result["status"] == "fail":
            red.append(f"{variant}: failed")
    exit_code = 2 if incomplete else 1 if red else 0
    return {
        "schema": "machina-audit-report/1",
        "verdict": VERDICTS[exit_code],
        "exit_code": exit_code,
        "incomplete": incomplete,
        "red": red,
        "expected": expected,
        "not_executed": not_executed(list(legs.values())),
        "legs": [legs[v] for v in sorted(legs)],
    }


def markdown(report: dict[str, Any]) -> str:
    """The step summary: verdict, one row per leg, then every finding."""
    lines = [f"# Quality audit: {report['verdict']} (exit {report['exit_code']})", ""]
    lines += [f"- incomplete: {reason}" for reason in report["incomplete"]]
    lines += [f"- red: {reason}" for reason in report["red"]]
    if report["not_executed"]:
        count = len(report["not_executed"])
        lines.append(f"- not covered: {count} test(s) or module(s) no leg executed (listed below)")
    lines += [
        "",
        "| Leg | Check | Status | Executed | Skipped | Findings |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    by_variant = {leg["variant"]: leg for leg in report["legs"]}
    for variant in sorted(set(report["expected"]) | set(by_variant)):
        leg = by_variant.get(variant)
        if leg is None:
            lines.append(f"| {variant} | | **missing** | | | |")
            continue
        note = " (informational)" if leg["informational"] else ""
        lines.append(
            f"| {variant}{note} | {leg['check']} | {leg['status']} | {leg['facts']['executed']} "
            f"| {leg['facts'].get('skipped', '')} | {len(leg['findings'])} |"
        )
    if report["not_executed"]:
        lines += ["", "## Not executed in any leg", ""]
        lines += ["Every suite leg with test results skipped these, so nothing here is covered:"]
        lines.append("")
        for entry in report["not_executed"]:
            scope = " (whole module)" if entry["whole_module"] else ""
            reasons = "; ".join(f"{r} ({where})" for r, where in entry["reasons"].items())
            lines.append(f"- `{entry['test']}`{scope}: {reasons}")
    for leg in report["legs"]:
        notes = [f"- floors: {leg['facts']['floors']}"] if "floors" in leg["facts"] else []
        notes += [f"- note: {note}" for note in leg["facts"].get("floor_notes", [])]
        if leg["findings"] or notes:
            lines += ["", f"## {leg['variant']}", "", *notes]
            for item in leg["findings"]:
                lines.append(f"- `{item['rule']}` {item['message']}")
                if item.get("detail"):
                    lines += [
                        "",
                        "  ```text",
                        *(f"  {d}" for d in item["detail"][-1500:].splitlines()),
                        "  ```",
                    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--expected", required=True, help="leg variants, space-separated")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)

    report = build(args.results, args.expected.split())
    text = markdown(report)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "audit-report.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    (args.out / "audit-report.md").write_text(text, encoding="utf-8")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write(text)
    print(text)
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
