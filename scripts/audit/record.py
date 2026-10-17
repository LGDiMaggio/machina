"""Write one audit leg's result file, the only thing the report job reads.

Each leg of .github/workflows/audit.yml ends with a step that runs under
``if: always()``, so a leg leaves a result even when its install or test
step failed::

    record.py suite --variant newest-py3.12 --junit junit.xml \\
        --exit-code-file pytest-exit-code --out audit-results/result.json
    record.py security --audit core=core.json --audit all=all.json \\
        --out audit-results/result.json

A result is ``{schema, check, variant, status, informational, facts,
findings}``. ``status`` is pass, fail or error (the check could not run).
``facts["executed"]`` counts the tests, checks or packages the leg covered;
the report treats zero as an incomplete run. Both subcommands exit 0, 1 or 2
for pass, fail or error, so the job's own status matches its result.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCHEMA = "machina-audit-result/1"
EXIT_CODES = {"pass": 0, "fail": 1, "error": 2}
MAX_LISTED = 50  # failed tests listed one by one; facts carry the full count
DETAIL_LIMIT = 3000


def finding(rule: str, message: str, detail: str = "") -> dict[str, str]:
    """One problem a leg found; ``detail`` is a truncated evidence excerpt."""
    entry = {"rule": rule, "message": message}
    if detail:
        entry["detail"] = detail[-DETAIL_LIMIT:]
    return entry


def write_result(
    out: Path,
    check: str,
    variant: str,
    status: str,
    facts: dict[str, Any],
    findings: list[dict[str, str]],
    informational: bool = False,
) -> int:
    """Write the result file and return the exit code for ``status``."""
    document = {
        "schema": SCHEMA,
        "check": check,
        "variant": variant,
        "status": status,
        "informational": informational,
        "facts": {"python": platform.python_version(), "platform": sys.platform, **facts},
        "findings": findings,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{variant}: {status}, {facts.get('executed', 0)} executed, {len(findings)} findings")
    return EXIT_CODES[status]


def _tail(path: Path | None, lines: int = 25) -> str:
    if path is None or not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _cases(junit: Path) -> list[tuple[str, str, str, str]] | None:
    """Return ``(classname, name, outcome, message)`` per test case, or None."""
    try:
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return None
    cases = []
    for case in root.iter("testcase"):
        outcome, message = "passed", ""
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome, message = child.tag, child.get("message") or (child.text or "")
                break
        cases.append((case.get("classname", ""), case.get("name", ""), outcome, message))
    return cases


def _matches(classname: str, name: str, node: str) -> bool:
    """Whether a JUnit case belongs to a pytest node id such as ``a/b.py::C::t``."""
    path, *parts = node.split("::")
    prefix = ".".join([path.removesuffix(".py").replace("/", "."), *parts[:-1]])
    if not parts:
        return classname == prefix or classname.startswith(prefix + ".")
    return classname == prefix and (name == parts[-1] or name.startswith(parts[-1] + "["))


def suite(args: argparse.Namespace) -> int:
    facts: dict[str, Any] = {"executed": 0}
    for fact in args.fact:
        key, _, value = fact.partition("=")
        if value:
            facts[key] = value
    findings: list[dict[str, str]] = []

    def done(status: str) -> int:
        return write_result(
            args.out, "suite", args.variant, status, facts, findings, args.informational
        )

    code_file = Path(args.exit_code_file)
    exit_code = int(code_file.read_text().strip()) if code_file.is_file() else None
    cases = _cases(Path(args.junit))
    if cases is None or exit_code is None:
        detail = _tail(Path(args.install_log) if args.install_log else None)
        message = "pytest left no JUnit report or exit code: setup failed before it finished"
        findings.append(finding("no-test-results", message, detail))
        return done("error")

    failed = [case for case in cases if case[2] in ("failure", "error")]
    facts.update(
        pytest_exit_code=exit_code,
        tests=len(cases),
        executed=sum(case[2] != "skipped" for case in cases),
        skipped=sum(case[2] == "skipped" for case in cases),
        failed=len(failed),
    )
    for classname, name, outcome, message in failed[:MAX_LISTED]:
        findings.append(finding(f"test-{outcome}", f"{classname}::{name}", message))
    # pytest exits 1 on failed tests and 2 on collection errors; anything else
    # (interrupted, internal error, usage error, nothing collected) is a run
    # that could not judge the code.
    status = "pass" if exit_code == 0 else "fail" if exit_code == 1 or failed else "error"
    for node in args.require:
        if not any(c[2] != "skipped" and _matches(c[0], c[1], node) for c in cases):
            findings.append(finding("required-suite-skipped", node, "no test in it ran"))
            status = "fail" if status == "pass" else status
    return done(status)


def security(args: argparse.Namespace) -> int:
    status, facts, findings = "pass", {"executed": 0}, []
    advisories: dict[tuple[str, str, str], dict[str, Any]] = {}
    for spec in args.audit:
        label, _, path = spec.partition("=")
        try:
            dependencies = json.loads(Path(path).read_text(encoding="utf-8"))["dependencies"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status = "error"
            findings.append(finding("audit-unreadable", f"{label}: no pip-audit JSON", repr(exc)))
            continue
        audited = [dep for dep in dependencies if "skip_reason" not in dep]
        facts[f"{label}_audited"] = len(audited)
        facts[f"{label}_skipped"] = len(dependencies) - len(audited)
        facts["executed"] += len(audited)
        if not audited:
            status = "error"
            findings.append(finding("audit-empty", f"{label}: pip-audit audited nothing"))
        for dep in audited:
            for vuln in dep.get("vulns", []):
                key = (dep["name"], dep["version"], vuln["id"])
                entry = advisories.setdefault(key, {"vuln": vuln, "labels": []})
                entry["labels"].append(label)
    for (name, version, vuln_id), entry in sorted(advisories.items()):
        fixes = ", ".join(entry["vuln"].get("fix_versions") or []) or "none yet"
        detail = f"resolutions: {', '.join(entry['labels'])}; fixed in: {fixes}"
        findings.append(finding("vulnerability", f"{name} {version}: {vuln_id}", detail))
    facts["vulnerabilities"] = len(advisories)
    if status == "pass" and advisories:
        status = "fail"
    return write_result(args.out, "security", "security", status, facts, findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    suite_parser = commands.add_parser("suite", help="a pytest leg, from its JUnit XML")
    suite_parser.add_argument("--variant", required=True)
    suite_parser.add_argument("--junit", required=True)
    suite_parser.add_argument("--exit-code-file", required=True)
    suite_parser.add_argument("--install-log", help="tail quoted when pytest never ran")
    suite_parser.add_argument("--require", action="append", default=[], help="pytest node id")
    suite_parser.add_argument("--fact", action="append", default=[], help="key=value")
    suite_parser.add_argument("--informational", action="store_true")
    suite_parser.set_defaults(func=suite)
    security_parser = commands.add_parser("security", help="pip-audit JSON files")
    security_parser.add_argument("--audit", action="append", required=True, help="label=path")
    security_parser.set_defaults(func=security)
    for sub in (suite_parser, security_parser):
        sub.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
