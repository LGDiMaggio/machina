"""Write one audit leg's result file, the only thing the report job reads.

Each leg of .github/workflows/audit.yml ends with a step that runs under
``if: always()``, so a leg leaves a result even when its install or test
step failed::

    record.py suite --variant newest-py3.12 --junit junit.xml \\
        --exit-code-file pytest-exit-code --out audit-results/result.json
    record.py security --audit core=core.json --audit all=all.json \\
        --out audit-results/result.json

A result is ``{schema, check, variant, status, informational, facts,
findings}``. ``status`` is pass, fail or error (the check could not judge
what it exists to judge: no test results, a required suite that never ran,
floors that were not the ones installed, an audit file that is missing).
``facts["executed"]`` counts the tests, checks or packages the leg covered;
the report treats zero as an incomplete run. A suite result also lists every
skipped test with its reason, so a skip is never counted as coverage. Both
subcommands exit 0, 1 or 2 for pass, fail or error, so the job's own status
matches its result.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from importlib import metadata
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
        "facts": {
            "python": platform.python_version(),
            "platform": sys.platform,
            "executed": 0,  # the report requires it, even on a result that ran nothing
            **facts,
        },
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
    # A failure or error outranks a skip in the same case (a skipped test whose
    # teardown errored); an xfail ran, so it is not a skip.
    rank = {"failure": 2, "error": 2, "skipped": 1, "xfailed": 1}
    cases = []
    for case in root.iter("testcase"):
        outcome, message = "passed", ""
        for child in case:
            tag = child.tag
            if tag == "skipped" and child.get("type") == "pytest.xfail":
                tag = "xfailed"
            if rank.get(tag, 0) <= rank.get(outcome, 0):
                continue
            outcome, message = tag, child.get("message") or (child.text or "")
            # A module skipped at import is one case whose message is only
            # "collection skipped"; the reason is inside its text.
            reason = re.search(r"Skipped: (.*?)['\"]\)\s*$", child.text or "")
            if message == "collection skipped" and reason:
                message = reason[1]
        cases.append((case.get("classname", ""), case.get("name", ""), outcome, message))
    return cases


def _test_id(classname: str, name: str) -> str:
    return f"{classname}::{name}" if classname else name


def test_module(test_id: str) -> str:
    """The dotted module of a test id; a module-level case's id is the module."""
    parts = test_id.split("::")[0].split(".")
    while parts and parts[-1][:1].isupper():  # drop test classes (CapWords)
        parts.pop()
    return ".".join(parts)


def _module(classname: str, name: str) -> str:
    return test_module(_test_id(classname, name))


def _core_versions() -> dict[str, str]:
    """The installed version of each core dependency of machina-ai."""
    try:
        requirements = metadata.requires("machina-ai") or []
    except metadata.PackageNotFoundError:
        return {}
    versions = {}
    for requirement in requirements:
        name = re.match(r"[A-Za-z0-9._-]+", requirement)
        if name and not re.search(r"extra\s*==", requirement):
            try:
                versions[name[0]] = metadata.version(name[0])
            except metadata.PackageNotFoundError:
                versions[name[0]] = "missing"
    return versions


def _matches(classname: str, name: str, node: str) -> bool:
    """Whether a JUnit case is in a pytest node: ``a/b.py``, ``a/b.py::C``, ``a/b.py::C::t``.

    A node naming one test matches all of its parametrizations, unless it
    names one itself (``a/b.py::t[x]``).
    """
    base, bracket, params = node.partition("[")
    path, *parts = base.split("::")
    target = ".".join([path.removesuffix(".py").replace("/", "."), *parts])
    test, _, case_params = name.partition("[")
    if bracket and case_params != params:
        return False
    case = f"{classname}.{test}" if classname else name
    return case == target or case.startswith(target + ".")


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

    if args.facts_json and Path(args.facts_json).is_file():
        facts.update(json.loads(Path(args.facts_json).read_text(encoding="utf-8")))
    facts["core_versions"] = _core_versions()
    code_file = Path(args.exit_code_file)
    code_text = code_file.read_text().strip() if code_file.is_file() else ""
    exit_code = int(code_text) if code_text.isdigit() else None
    cases = _cases(Path(args.junit))
    if cases is None or exit_code is None:
        detail = _tail(Path(args.install_log) if args.install_log else None)
        message = "pytest left no JUnit report or exit code: setup failed before it finished"
        findings.append(finding("no-test-results", message, detail))
        return done("error")

    failed = [case for case in cases if case[2] in ("failure", "error")]
    skipped = [case for case in cases if case[2] == "skipped"]
    # A collection error is one case with no classname standing for a module
    # that never ran: it is a failure, not an executed test.
    ran = [case for case in cases if case[2] != "skipped" and (case[0] or case[2] != "error")]
    facts.update(
        pytest_exit_code=exit_code,
        tests=len(cases),
        executed=len(ran),
        skipped=len(skipped),
        failed=len(failed),
        # Every skip with its reason, and executed tests per module, so the
        # report can name what no leg executed instead of counting it covered.
        skipped_tests=[{"test": _test_id(c, n), "reason": r[:300]} for c, n, _, r in skipped],
        executed_by_module=Counter(_module(c, n) for c, n, _, _ in ran),
    )
    for classname, name, outcome, message in failed[:MAX_LISTED]:
        findings.append(finding(f"test-{outcome}", _test_id(classname, name), message))
    # pytest exits 1 on failed tests and 2 on collection errors; anything else
    # (interrupted, internal error, usage error, nothing collected) is a run
    # that could not judge the code.
    status = "pass" if exit_code == 0 else "fail" if exit_code == 1 or failed else "error"
    if exit_code == 1 and not failed:
        findings.append(finding("pytest-failed", "pytest exited 1 with no failed test case"))
    # A suite this leg exists to run that did not run leaves the leg unable to
    # judge it: incomplete, whatever the other tests did.
    for node in args.require:
        if not any(_matches(c[0], c[1], node) for c in ran):
            module = node.split("::")[0].removesuffix(".py").replace("/", ".")
            reasons = sorted({r for c, n, _, r in skipped if _module(c, n) == module})
            findings.append(finding("required-suite-not-run", node, "; ".join(reasons)))
            status = "error"
    if args.expect_pins:
        status = _check_pins(Path(args.expect_pins), findings, status)
    return done(status)


def _check_pins(pins: Path, findings: list[dict[str, str]], status: str) -> str:
    """The floors leg judged the floors only if pip installed exactly those pins."""
    lines = pins.read_text(encoding="utf-8").split() if pins.is_file() else []
    for line in lines:
        name, _, version = line.partition("==")
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = "missing"
        if installed != version:
            findings.append(finding("pin-not-installed", f"{name}=={version}", installed))
            status = "error"
    if not lines:
        findings.append(finding("pin-not-installed", f"{pins}: no pins to check"))
        status = "error"
    return status


def security(args: argparse.Namespace) -> int:
    status, facts, findings = "pass", {"executed": 0}, []
    advisories: dict[tuple[str, str, str], dict[str, Any]] = {}
    for spec in args.audit:
        label, _, path = spec.partition("=")
        try:
            dependencies = json.loads(Path(path).read_text(encoding="utf-8"))["dependencies"]
            audited = [dep for dep in dependencies if "skip_reason" not in dep]
            names = {dep["name"].lower().replace("_", "-") for dep in dependencies}
            found = [((d["name"], d["version"], v["id"]), v) for d in audited for v in d["vulns"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status = "error"
            findings.append(
                finding("audit-unreadable", f"{label}: no usable pip-audit JSON", repr(exc))
            )
            continue
        facts[f"{label}_audited"] = len(audited)
        facts[f"{label}_skipped"] = len(dependencies) - len(audited)
        facts["executed"] += len(audited)
        # A failed install leaves a venv holding little more than pip, which
        # audits clean; only an environment that contains machina-ai counts.
        if "machina-ai" not in names:
            status = "error"
            findings.append(finding("audit-incomplete", f"{label}: machina-ai is not installed"))
        for key, vuln in found:
            # pip-audit can list one advisory twice for a dependency.
            entry = advisories.setdefault(key, {"vuln": vuln, "labels": {}})
            entry["labels"][label] = None
    for (name, version, vuln_id), entry in sorted(advisories.items()):
        fixes = ", ".join(entry["vuln"].get("fix_versions") or []) or "none yet"
        aliases = ", ".join(entry["vuln"].get("aliases") or []) or "none"
        detail = (
            f"resolutions: {', '.join(entry['labels'])}; fixed in: {fixes}; aliases: {aliases}"
        )
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
    suite_parser.add_argument("--facts-json", help="JSON object merged into facts, if present")
    suite_parser.add_argument("--expect-pins", help="name==version lines that must be installed")
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
