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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "machina-audit-result/1"
CHECKS = ("suite", "package-smoke", "security")
STATUSES = ("pass", "fail", "error")
VERDICTS = {0: "green", 1: "red", 2: "incomplete"}


def problems(result: Any) -> list[str]:
    """Why ``result`` does not follow the result schema; empty when it does."""
    if not isinstance(result, dict):
        return ["not a JSON object"]
    facts, findings = result.get("facts"), result.get("findings")
    executed = facts.get("executed") if isinstance(facts, dict) else None
    rules = {
        f"schema is not {SCHEMA}": result.get("schema") == SCHEMA,
        f"check is not one of {CHECKS}": result.get("check") in CHECKS,
        "variant is empty": isinstance(result.get("variant"), str) and result["variant"],
        f"status is not one of {STATUSES}": result.get("status") in STATUSES,
        "informational is not a boolean": isinstance(result.get("informational"), bool),
        "facts.executed is not a count": type(executed) is int and executed >= 0,
        "findings are not {rule, message} objects": isinstance(findings, list)
        and all(isinstance(f, dict) and {"rule", "message"} <= f.keys() for f in findings),
    }
    return [message for message, ok in rules.items() if not ok]


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
        "legs": [legs[v] for v in sorted(legs)],
    }


def markdown(report: dict[str, Any]) -> str:
    """The step summary: verdict, one row per leg, then every finding."""
    lines = [f"# Quality audit: {report['verdict']} (exit {report['exit_code']})", ""]
    lines += [f"- incomplete: {reason}" for reason in report["incomplete"]]
    lines += [f"- red: {reason}" for reason in report["red"]]
    lines += [
        "",
        "| Leg | Check | Status | Executed | Findings |",
        "| --- | --- | --- | --- | --- |",
    ]
    by_variant = {leg["variant"]: leg for leg in report["legs"]}
    for variant in sorted(set(report["expected"]) | set(by_variant)):
        leg = by_variant.get(variant)
        if leg is None:
            lines.append(f"| {variant} | | **missing** | | |")
            continue
        note = " (informational)" if leg["informational"] else ""
        lines.append(
            f"| {variant}{note} | {leg['check']} | {leg['status']} "
            f"| {leg['facts']['executed']} | {len(leg['findings'])} |"
        )
    for leg in report["legs"]:
        if leg["findings"]:
            lines += ["", f"## {leg['variant']}", ""]
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
