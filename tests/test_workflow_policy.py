"""Pin the safety and completeness rules of the GitHub Actions workflows.

The weekly audit (.github/workflows/audit.yml) and the weekly litellm check
(.github/workflows/litellm-compat.yml) only run on a schedule, on demand, or
when their own files change, so a pull request that breaks one of their rules
would otherwise go unnoticed until the next Monday. The distribution-evidence
workflow (.github/workflows/dist-evidence.yml) runs only on demand or when its
own files change, and must never be able to publish.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
AUDIT = WORKFLOWS / "audit.yml"


def _load(path: Path) -> dict[Any, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a YAML mapping"
    return document


def _keys(node: Any) -> list[str]:
    """Every mapping key anywhere in ``node``."""
    if isinstance(node, dict):
        return [str(key) for key in node] + [k for value in node.values() for k in _keys(value)]
    if isinstance(node, list):
        return [k for item in node for k in _keys(item)]
    return []


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_no_continue_on_error(path: Path) -> None:
    # A step or job that may fail silently hides the status the audit reports.
    assert "continue-on-error" not in _keys(_load(path))


def test_audit_permissions_are_read_only() -> None:
    audit = _load(AUDIT)
    assert audit.get("permissions") == {"contents": "read"}
    for name, job in audit["jobs"].items():
        assert "permissions" not in job, f"job {name} widens the top-level permissions"
    assert "secrets." not in AUDIT.read_text(encoding="utf-8")


def test_audit_triggers() -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = _load(AUDIT)[True]
    assert set(triggers) == {"schedule", "workflow_dispatch", "pull_request"}
    assert not triggers["workflow_dispatch"], "manual runs take no inputs"
    assert set(triggers["pull_request"]) == {"paths"}


def test_audit_tools_are_pinned() -> None:
    # uv picks the floors and pip-audit judges vulnerabilities: a new release
    # of either could change the verdict without any change in this repo.
    text = AUDIT.read_text(encoding="utf-8")
    for tool in ("uv", "pip-audit"):
        assert re.search(rf'"{tool}==\d+(\.\d+)+"', text), f"{tool} is not pinned in audit.yml"


def test_every_audit_job_has_a_timeout() -> None:
    for name, job in _load(AUDIT)["jobs"].items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, f"job {name}: {timeout!r}"


def test_expected_legs_match_the_matrix() -> None:
    jobs = _load(AUDIT)["jobs"]
    report = jobs.pop("report")
    legs = []
    for name, job in jobs.items():
        strategy = job.get("strategy", {})
        include = strategy.get("matrix", {}).get("include")
        if include:
            assert strategy.get("fail-fast") is False, f"job {name} must not fail fast"
            legs += [entry["variant"] for entry in include]
        else:
            legs.append(name)
    (expected,) = [
        step["env"]["EXPECTED_LEGS"].split()
        for step in report["steps"]
        if "EXPECTED_LEGS" in step.get("env", {})
    ]
    assert sorted(expected) == sorted(legs)
    assert len(set(expected)) == len(expected)
    assert sorted(report["needs"]) == sorted(jobs)
    assert report.get("if") == "always()", "the report must run after failed legs"


# The constraints leg audits the versions deploy/docker/constraints.txt pins,
# as committed: the security leg audits fresh resolutions, which never see a
# pin they no longer pick. It installs pip-audit only, resolves and installs
# nothing from the file, never writes it, and is recorded like any other leg.
CONSTRAINTS = "deploy/docker/constraints.txt"
AUDIT_SCRIPTS = WORKFLOWS.parents[1] / "scripts" / "audit"


def _job_runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_constraints_leg_audits_the_committed_pins_without_installing_them() -> None:
    jobs = _load(AUDIT)["jobs"]
    job = jobs["constraints"]
    runs = _job_runs(job)
    text = "\n".join(runs)
    installs = [
        line.strip()
        for run in runs
        for line in run.splitlines()
        if re.search(r"\bpip\b.*\binstall\b", line)
    ]
    match = re.search(r'"pip-audit==\d+(\.\d+)+"', "\n".join(_job_runs(jobs["security"])))
    assert match, "the security leg pins pip-audit"
    assert installs == [f"python -m pip install --upgrade pip {match[0]}"], "pip-audit only"
    audits = [line.strip() for run in runs for line in run.splitlines() if "pip-audit -" in line]
    assert len(audits) == 1, "one pip-audit command"
    # --no-deps alone still installs the file into a virtualenv; with
    # --disable-pip, pip-audit reads the pins and installs nothing.
    assert audits[0].startswith('pip-audit -r "$layer" --no-deps --disable-pip -f json -o ')
    # A command substitution between pip-audit and `$?` would log its status instead.
    assert re.search(r'\|\| echo "pip-audit \(\$\{layer##\*/\}\) exited with status \$\?"', text)
    assert "--fix" not in text, "the leg never changes a pin"
    # The file is only ever read: split into layers, then checked for coverage.
    uses = re.findall(rf"(\S+)\s+{re.escape(CONSTRAINTS)}", text)
    assert sorted(uses) == ["--constraints", "scripts/audit/pins.py"], uses
    for run in runs:
        assert "${{" not in run, run
        assert "GITHUB_ENV" not in run and "GITHUB_PATH" not in run, run
        assert not re.search(r"\b(git|uv|curl|wget)\b", run), run
    for step in job["steps"]:
        uses_action = str(step.get("uses", ""))
        if uses_action:
            assert re.fullmatch(r"actions/[\w-]+@v\d+", uses_action), uses_action
        if uses_action.startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


def test_constraints_leg_is_recorded_like_the_other_legs() -> None:
    job = _load(AUDIT)["jobs"]["constraints"]
    assert "permissions" not in job and "continue-on-error" not in _keys(job)
    records = [step for step in job["steps"] if "record.py security" in step.get("run", "")]
    assert len(records) == 1, "one step judges the leg"
    assert records[0].get("if") == "always()", "a failed audit step is reported, not dropped"
    command = " ".join(records[0]["run"].replace("\\\n", " ").split())
    assert f"--variant constraints --constraints {CONSTRAINTS}" in command
    assert "--out audit-results/result.json" in command
    # The record step reads the layers where the audit step wrote them.
    (audit,) = [
        step["run"] for step in job["steps"] if "scripts/audit/pins.py" in step.get("run", "")
    ]
    assert f'pins.py {CONSTRAINTS} --out "$RUNNER_TEMP/pins"' in audit
    assert 'for layer in "$RUNNER_TEMP"/pins/layer-*.txt; do' in audit
    assert 'for layer in "$RUNNER_TEMP"/pins/layer-*.txt; do' in records[0]["run"]
    uploads = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert uploads[0].get("if") == "always()"
    assert uploads[0]["with"] == {
        "name": "leg-constraints",
        "path": "audit-results/",
        "if-no-files-found": "error",
    }


@pytest.fixture
def audit_scripts(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    """The audit scripts, importable as the workflow runs them (their directory first)."""
    import importlib

    monkeypatch.syspath_prepend(str(AUDIT_SCRIPTS))
    return tuple(importlib.import_module(name) for name in ("pins", "record", "report"))


SAMPLE_CONSTRAINTS = """\
# This file was autogenerated by uv via the following command:
#    uv pip compile --universal --python-version 3.11 pyproject.toml
chromadb==1.5.9
    # via machina-ai (pyproject.toml)
PyYAML==6.0.3
pywin32==312 ; sys_platform == 'win32'
soupsieve==2.10 ; python_full_version < '3.11.5'
    # via beautifulsoup4
soupsieve==4.0 ; python_full_version >= '3.11.5'
"""
SAMPLE_PINS = {
    "chromadb": {"1.5.9": ""},
    "pyyaml": {"6.0.3": ""},
    "pywin32": {"312": "sys_platform == 'win32'"},
    "soupsieve": {
        "2.10": "python_full_version < '3.11.5'",
        "4.0": "python_full_version >= '3.11.5'",
    },
}


def _pip_audit_json(path: Path, pins: list[str], vulns: dict[str, list[Any]]) -> None:
    """A pip-audit ``-f json`` file for ``name==version`` pins."""
    dependencies = []
    for pin in pins:
        name, version = pin.split("==")
        dependencies.append({"name": name, "version": version, "vulns": vulns.get(pin, [])})
    path.write_text(json.dumps({"dependencies": dependencies, "fixes": []}), encoding="utf-8")


def _constraints_leg(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any], drop: str = ""
) -> dict[str, Any]:
    """Run the leg's two scripts as the workflow does, with pip-audit's JSON faked."""
    pins, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(SAMPLE_CONSTRAINTS, encoding="utf-8")
    assert pins.main([str(constraints), "--out", str(tmp_path / "pins")]) == 0
    vulns = {
        "chromadb==1.5.9": [{"id": "GHSA-test", "fix_versions": [], "aliases": ["CVE-0-1"]}],
        # pip-audit can list one advisory twice for a dependency.
        "soupsieve==2.10": [{"id": "CVE-0-2", "fix_versions": ["3.0.2"], "aliases": []}] * 2,
    }
    args = ["security", "--variant", "constraints", "--constraints", str(constraints)]
    for layer in sorted((tmp_path / "pins").glob("*.txt")):
        lines = [pin for pin in layer.read_text(encoding="utf-8").split() if pin != drop]
        _pip_audit_json(layer.with_suffix(".json"), lines, vulns)
        args += ["--audit", f"{layer.stem}={layer.with_suffix('.json')}"]
    record.main([*args, "--out", str(tmp_path / "result.json")])
    result: dict[str, Any] = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    return result


def test_pins_writes_every_pinned_version_without_markers(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    pins, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(SAMPLE_CONSTRAINTS, encoding="utf-8")
    assert record.read_pins(constraints) == SAMPLE_PINS
    assert pins.main([str(constraints), "--out", str(tmp_path / "pins")]) == 0
    layers = sorted((tmp_path / "pins").glob("*.txt"))
    assert [layer.name for layer in layers] == ["layer-0.txt", "layer-1.txt"]
    contents = [layer.read_text(encoding="utf-8").split("\n") for layer in layers]
    assert contents == [
        ["chromadb==1.5.9", "pywin32==312", "pyyaml==6.0.3", "soupsieve==2.10", ""],
        ["soupsieve==4.0", ""],
    ]


@pytest.mark.parametrize(
    ("lines", "marker"),
    [
        (
            ["foo==1.0 ; sys_platform == 'linux'", "foo==1.0 ; sys_platform == 'darwin'"],
            "(sys_platform == 'linux') or (sys_platform == 'darwin')",
        ),
        (["foo==1.0 ; sys_platform == 'linux'", "foo==1.0"], ""),
        (["foo==1.0", "FOO==1.0 ; sys_platform == 'linux'"], ""),
    ],
)
def test_pins_reads_a_version_pinned_on_two_branches_once(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any], lines: list[str], marker: str
) -> None:
    _, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert record.read_pins(constraints) == {"foo": {"1.0": marker}}


@pytest.mark.parametrize(
    "line",
    ["requests>=2.0", "requests", "-e .", "httpx==0.28.1 \\", "pkg[extra]==1.0", "a==1 b==2"],
)
def test_pins_refuses_a_line_it_cannot_audit(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any], line: str
) -> None:
    # A requirement read as anything but one exact pin would leave the audit
    # silently short of it.
    pins, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(SAMPLE_CONSTRAINTS + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 10"):
        record.read_pins(constraints)
    assert pins.main([str(constraints), "--out", str(tmp_path / "pins")]) == 2
    assert not (tmp_path / "pins").exists()


def test_pins_refuses_a_file_without_pins(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    pins, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("# nothing pinned\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no name==version pins"):
        record.read_pins(constraints)
    assert pins.main([str(constraints), "--out", str(tmp_path / "pins")]) == 2
    assert pins.main([str(tmp_path / "missing.txt"), "--out", str(tmp_path / "pins")]) == 2


def test_committed_constraints_split_into_layers_pip_audit_accepts(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    # The leg reads the committed file: a format pins.py does not read (hashes,
    # extras, an unpinned line) fails here, not at the next weekly run.
    pins, record, _ = audit_scripts
    committed = WORKFLOWS.parents[1] / CONSTRAINTS
    pinned = record.read_pins(committed)
    assert len(pinned["soupsieve"]) == 2, "both sides of the soupsieve marker are audited"
    assert pins.main([str(committed), "--out", str(tmp_path)]) == 0
    seen = []
    for layer in sorted(tmp_path.glob("layer-*.txt")):
        names = [pin.split("==")[0] for pin in layer.read_text(encoding="utf-8").split()]
        assert len(names) == len(set(names)), f"{layer.name} pins a package twice"
        assert all(";" not in pin for pin in layer.read_text(encoding="utf-8").split())
        seen += layer.read_text(encoding="utf-8").split()
    assert sorted(seen) == sorted(f"{n}=={v}" for n, vs in pinned.items() for v in vs)


def test_constraints_result_follows_the_report_schema(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    _, _, report = audit_scripts
    result = _constraints_leg(tmp_path, audit_scripts)
    assert report.problems(result) == []
    assert (result["check"], result["variant"], result["status"]) == (
        "security",
        "constraints",
        "fail",
    )
    assert result["informational"] is False, "a pinned advisory counts toward the verdict"
    assert result["facts"]["executed"] == result["facts"]["pinned"] == 5
    assert result["facts"]["vulnerabilities"] == 2
    where = f"pinned in {tmp_path / 'constraints.txt'}"
    assert result["findings"] == [
        {
            "rule": "vulnerability",
            "message": "chromadb 1.5.9: GHSA-test",
            "detail": f"{where}; fixed in: none yet; aliases: CVE-0-1",
        },
        {
            "rule": "vulnerability",
            "message": "soupsieve 2.10: CVE-0-2",
            # The marker tells a reader which environments install the pin.
            "detail": f"{where} for python_full_version < '3.11.5'; fixed in: 3.0.2; aliases: none",
        },
    ]
    # The report merges it like any other leg: a pinned advisory makes it red.
    (tmp_path / "results" / "leg-constraints").mkdir(parents=True)
    (tmp_path / "result.json").replace(tmp_path / "results" / "leg-constraints" / "result.json")
    built = report.build(tmp_path / "results", ["constraints"])
    assert (built["verdict"], built["red"]) == ("red", ["constraints: failed"])


def _record_security(
    record: Any, tmp_path: Path, labels: list[str], *options: str
) -> tuple[int, dict[str, Any]]:
    """Run ``record.py security`` on the ``<label>.json`` files in ``tmp_path``."""
    audits = [arg for label in labels for arg in ("--audit", f"{label}={tmp_path / label}.json")]
    out = tmp_path / "result.json"
    code = record.main(["security", *options, *audits, "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_constraints_result_passes_when_no_pin_has_an_advisory(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    _, record, report = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("pyyaml==6.0.3\n", encoding="utf-8")
    _pip_audit_json(tmp_path / "layer-0.json", ["pyyaml==6.0.3"], {})
    options = ("--variant", "constraints", "--constraints", str(constraints))
    code, result = _record_security(record, tmp_path, ["layer-0"], *options)
    assert (code, result["status"], result["findings"]) == (0, "pass", [])
    assert report.problems(result) == []


@pytest.mark.parametrize("drop", ["soupsieve==4.0", "pywin32==312"])
def test_constraints_result_is_an_error_when_a_pin_was_not_audited(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any], drop: str
) -> None:
    # A pin missing from pip-audit's output is not a clean pin.
    _, _, report = audit_scripts
    result = _constraints_leg(tmp_path, audit_scripts, drop=drop)
    assert result["status"] == "error"
    assert report.problems(result) == []
    incomplete = [f for f in result["findings"] if f["rule"] == "audit-incomplete"]
    assert incomplete == [
        {
            "rule": "audit-incomplete",
            "message": "1 of 5 pinned versions were not audited",
            "detail": drop,
        }
    ]


def test_constraints_result_is_an_error_without_a_layer_audit(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    # pip-audit failed on a layer (or the split failed): its JSON is missing.
    _, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(SAMPLE_CONSTRAINTS, encoding="utf-8")
    _pip_audit_json(tmp_path / "layer-0.json", ["soupsieve==4.0"], {})
    options = ("--variant", "constraints", "--constraints", str(constraints))
    code, result = _record_security(record, tmp_path, ["layer-0", "layer-1"], *options)
    assert (code, result["status"]) == (2, "error")
    rules = [f["rule"] for f in result["findings"]]
    assert rules == ["audit-unreadable", "audit-incomplete"]
    assert result["findings"][1]["message"] == "4 of 5 pinned versions were not audited"


def test_constraints_result_is_an_error_when_pip_audit_skipped_a_pin(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    # pip-audit lists a version it could not look up with a skip_reason.
    _, record, _ = audit_scripts
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("pyyaml==6.0.3\nnot-on-pypi==1.0\n", encoding="utf-8")
    skipped = {"name": "not-on-pypi", "skip_reason": "Dependency not found on PyPI"}
    audited = {"name": "pyyaml", "version": "6.0.3", "vulns": []}
    (tmp_path / "layer-0.json").write_text(
        json.dumps({"dependencies": [audited, skipped], "fixes": []}), encoding="utf-8"
    )
    options = ("--variant", "constraints", "--constraints", str(constraints))
    code, result = _record_security(record, tmp_path, ["layer-0"], *options)
    assert (code, result["status"], result["facts"]["layer-0_skipped"]) == (2, "error", 1)
    assert result["findings"] == [
        {
            "rule": "audit-incomplete",
            "message": "1 of 2 pinned versions were not audited",
            "detail": "not-on-pypi==1.0",
        }
    ]


def test_constraints_result_is_an_error_for_an_unreadable_constraints_file(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    _, record, report = audit_scripts
    _pip_audit_json(tmp_path / "layer-0.json", ["pyyaml==6.0.3"], {})
    options = ("--variant", "constraints", "--constraints", str(tmp_path / "none.txt"))
    code, result = _record_security(record, tmp_path, ["layer-0"], *options)
    assert (code, result["status"]) == (2, "error")
    assert result["findings"][0]["rule"] == "constraints-unreadable"
    assert report.problems(result) == []


def test_security_leg_still_requires_machina_ai_in_each_resolution(
    tmp_path: Path, audit_scripts: tuple[Any, Any, Any]
) -> None:
    # The fresh-resolution leg keeps its own completeness rule and variant.
    _, record, _ = audit_scripts
    _pip_audit_json(tmp_path / "core.json", ["machina-ai==0.4.0", "pyyaml==6.0.3"], {})
    _pip_audit_json(tmp_path / "all.json", ["pip==26.1"], {})
    code, result = _record_security(record, tmp_path, ["core", "all"])
    assert (code, result["variant"], result["status"]) == (2, "security", "error")
    assert result["findings"] == [
        {"rule": "audit-incomplete", "message": "all: machina-ai is not installed"}
    ]
    assert "pinned" not in result["facts"]


# The weekly litellm check (.github/workflows/litellm-compat.yml) installs the
# litellm extra without mcp and requires the real-litellm tool-call test (#160).
LITELLM_CHECK = WORKFLOWS / "litellm-compat.yml"
CONTRACT_TEST = (
    "tests/unit/test_llm_provider.py::TestLiteLLMToolCallContract"
    "::test_complete_with_tools_returns_tool_call_from_real_litellm"
)


def _steps(path: Path) -> list[dict[str, Any]]:
    return [step for job in _load(path)["jobs"].values() for step in job["steps"]]


def test_litellm_check_runs_weekly_on_demand_and_read_only() -> None:
    workflow = _load(LITELLM_CHECK)
    triggers = workflow[True]
    assert set(triggers) == {"schedule", "workflow_dispatch", "pull_request"}
    assert [entry["cron"] for entry in triggers["schedule"]], "the check runs on a schedule"
    assert not triggers["workflow_dispatch"], "manual runs take no inputs"
    assert set(triggers["pull_request"]) == {"paths"}
    assert workflow.get("permissions") == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"job {name} widens the top-level permissions"
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, f"job {name}: {timeout!r}"
    assert "secrets." not in LITELLM_CHECK.read_text(encoding="utf-8")


def test_litellm_check_installs_the_extra_without_mcp() -> None:
    runs = [step.get("run", "") for step in _steps(LITELLM_CHECK)]
    installs = [run for run in runs if "pip install -e" in run]
    assert installs, "the check installs the package"
    for run in installs:
        assert '-e ".[dev,litellm]"' in run and "mcp" not in run, run
    # The guard fails the job when mcp is installed or litellm does not import.
    guard = next((run for run in runs if 'find_spec("mcp")' in run), "")
    assert guard, "no step checks that mcp is absent"
    assert 'import_module("litellm")' in guard


def test_litellm_check_requires_the_contract_test() -> None:
    records = [step for step in _steps(LITELLM_CHECK) if "record.py suite" in step.get("run", "")]
    assert len(records) == 1, "one step judges the run"
    assert records[0].get("if") == "always()", "the judgement runs after a failed step"
    assert f"--require {CONTRACT_TEST}" in records[0]["run"]
    # Only the contract test runs, so the status reflects litellm alone.
    pytest_runs = [
        step["run"] for step in _steps(LITELLM_CHECK) if "-m pytest" in step.get("run", "")
    ]
    assert len(pytest_runs) == 1, "one step runs pytest"
    target = re.escape(CONTRACT_TEST.rsplit("::", 1)[0])
    assert re.search(rf"-m pytest\s+{target}\s", pytest_runs[0]), pytest_runs[0]


def _markers(node: ast.AST) -> set[str]:
    """The last name of each decorator, e.g. ``xfail`` for ``@pytest.mark.xfail(...)``."""
    decorators = getattr(node, "decorator_list", [])
    return {
        ast.unparse(d.func if isinstance(d, ast.Call) else d).split(".")[-1] for d in decorators
    }


def test_litellm_check_contract_test_exists_and_is_not_skipped() -> None:
    # A renamed or moved test leaves the check incomplete only at the next
    # weekly run, and record.py counts an xfail as run, so an xfail marker
    # would let a broken litellm pass.
    path, cls, name = CONTRACT_TEST.split("::")
    tree = ast.parse((WORKFLOWS.parents[1] / path).read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls]
    assert len(classes) == 1, f"{cls} is not a top-level class of {path}"
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(methods) == 1, f"{name} is not a method of {cls}"
    skips = {"skip", "skipif", "xfail"}
    assert not (_markers(classes[0]) | _markers(methods[0])) & skips
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            assert not any(skip in ast.unparse(node.value) for skip in skips)


# The distribution-evidence workflow builds the wheel and sdist, checks their
# LICENSE and writes SBOMs and notices bundles as run artifacts. Its pull-request
# trigger covers only its own files, and nothing in it may publish or release.
DIST_EVIDENCE = WORKFLOWS / "dist-evidence.yml"
DIST_EVIDENCE_PATHS = [
    ".github/workflows/dist-evidence.yml",
    "scripts/compliance/**",
    "tests/unit/test_verify_dist.py",
    "tests/unit/test_collect_notices.py",
]
# The install profiles of the compliance plan (KTD7), by label: extras.
SBOM_PROFILES = {
    "core": "",
    "all": "all",
    "docs-rag-pro": "docs-rag-pro",
    "sql-jdbc": "sql-jdbc",
    "deploy-image": "cmms-rest,mcp",
    "template-image": "excel,litellm",
}
PUBLISHING = (
    "twine",
    "pypi",
    "action-gh-release",
    "github-script",
    "gh release",
    "gh api",
    "git tag",
    "git push",
    "docker push",
    "docker login",
)
# The only installs: the two pinned tools, and the built wheel into the profile venv.
PINNED_INSTALL = re.compile(r'python -m pip install "(build|cyclonedx-bom)==\d+(\.\d+)+"')
WHEEL_INSTALL = (
    'python -m pip --python "$RUNNER_TEMP/profile/bin/python" install --no-cache-dir "$target"'
)


def _runs(path: Path) -> list[str]:
    return [step["run"] for step in _steps(path) if "run" in step]


def test_dist_evidence_permissions_are_read_only() -> None:
    workflow = _load(DIST_EVIDENCE)
    assert workflow.get("permissions") == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"job {name} widens the top-level permissions"
    # secrets.X, secrets['X'] and toJSON(secrets) alike.
    assert not re.search(r"\bsecrets\b", DIST_EVIDENCE.read_text(encoding="utf-8"))
    for step in _steps(DIST_EVIDENCE):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


def test_dist_evidence_triggers() -> None:
    triggers = _load(DIST_EVIDENCE)[True]
    assert set(triggers) == {"workflow_dispatch", "pull_request"}
    assert not triggers["workflow_dispatch"], "manual runs take no inputs"
    assert triggers["pull_request"] == {"paths": DIST_EVIDENCE_PATHS}


def test_dist_evidence_tools_are_pinned() -> None:
    # build makes the distributions and cyclonedx-bom the SBOMs: a new release
    # of either could change the evidence without any change in this repo.
    installs = [
        line.strip()
        for run in _runs(DIST_EVIDENCE)
        for line in run.splitlines()
        if re.search(r"\bpip\b.*\binstall\b", line)
    ]
    tools = set()
    for line in installs:
        if match := PINNED_INSTALL.fullmatch(line):
            tools.add(match.group(1))
        else:
            assert line == WHEEL_INSTALL, f"unpinned or unexpected install: {line}"
    assert tools == {"build", "cyclonedx-bom"}
    sbom = [run for run in _runs(DIST_EVIDENCE) if "cyclonedx-py environment" in run]
    assert len(sbom) == 1, "one step writes the SBOM"
    for flag in (
        "--pyproject pyproject.toml",
        "--mc-type library",
        "--sv 1.6",
        "--gather-license-texts",
        "--output-reproducible",
    ):
        assert flag in sbom[0], f"the SBOM command lacks {flag}"


def test_dist_evidence_runs_both_checks() -> None:
    jobs = _load(DIST_EVIDENCE)["jobs"]
    verify = [
        s for s in jobs["dist"]["steps"] if "scripts/compliance/verify_dist.py" in s.get("run", "")
    ]
    assert len(verify) == 1, "the dist job runs the LICENSE check once"
    assert "--dist dist --license LICENSE" in verify[0]["run"]
    assert verify[0].get("if") == "always()", "a failed build is reported by the check"
    collect = [
        s
        for s in jobs["sbom"]["steps"]
        if "scripts/compliance/collect_notices.py" in s.get("run", "")
    ]
    assert len(collect) == 1, "each profile collects its notices once"
    assert "--site-packages" in collect[0]["run"]


def test_every_dist_evidence_job_has_a_timeout() -> None:
    for name, job in _load(DIST_EVIDENCE)["jobs"].items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, f"job {name}: {timeout!r}"


def test_dist_evidence_cannot_publish() -> None:
    # Everything that can run (comments are not parsed), including with: and env:.
    content = str(_load(DIST_EVIDENCE)).lower()
    assert not [word for word in PUBLISHING if word in content]
    for step in _steps(DIST_EVIDENCE):
        uses = step.get("uses")
        if uses:
            assert re.fullmatch(r"actions/[\w-]+@v\d+", uses), (
                f"{uses}: not a GitHub-owned action at a release tag"
            )
    for run in _runs(DIST_EVIDENCE):
        # Expressions expanded into a script are an injection surface; GITHUB_ENV and
        # GITHUB_PATH would change what later steps run.
        assert "${{" not in run, run
        assert "GITHUB_ENV" not in run and "GITHUB_PATH" not in run, run


def test_dist_evidence_covers_the_planned_profiles() -> None:
    sbom = _load(DIST_EVIDENCE)["jobs"]["sbom"]
    assert sbom.get("needs") == "dist", "the SBOMs describe the wheel the dist job built"
    assert sbom["strategy"].get("fail-fast") is False, "one failed profile must not stop the rest"
    profiles = {
        entry["profile"]: entry["extras"] for entry in sbom["strategy"]["matrix"]["include"]
    }
    assert profiles == SBOM_PROFILES


def test_dist_evidence_uploads_keep_failure_evidence() -> None:
    uploads = [
        step
        for step in _steps(DIST_EVIDENCE)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 2, "one upload per job"
    for step in uploads:
        assert step.get("if") == "always()", f"{step['name']} must run after a failed step"
        assert step["with"].get("if-no-files-found") == "error"
        assert step["with"].get("retention-days") == 90
    # Only the distributions and the evidence, never the checkout or the runner's files.
    assert sorted(step["with"]["path"].split() for step in uploads) == [
        ["dist/", "evidence/"],
        ["evidence/"],
    ]


# The image-evidence workflow builds the three default images as they are and
# records what they contain. Like dist-evidence it must never be able to publish,
# and the one tool it downloads, syft, must be checked before it runs.
IMAGE_EVIDENCE = WORKFLOWS / "image-evidence.yml"
DEFAULT_IMAGES = [
    {"image": "machina", "dockerfile": "deploy/docker/Dockerfile", "context": "."},
    {
        "image": "mock-cmms",
        "dockerfile": "deploy/docker/mock-cmms/Dockerfile",
        "context": "deploy/docker/mock-cmms",
    },
    {
        "image": "template",
        "dockerfile": "templates/odl-generator-from-text/Dockerfile",
        "context": "templates/odl-generator-from-text",
    },
]


def test_image_evidence_permissions_are_read_only() -> None:
    workflow = _load(IMAGE_EVIDENCE)
    assert workflow.get("permissions") == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"job {name} widens the top-level permissions"
    assert not re.search(r"\bsecrets\b", IMAGE_EVIDENCE.read_text(encoding="utf-8"))
    for step in _steps(IMAGE_EVIDENCE):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


def test_image_evidence_triggers() -> None:
    triggers = _load(IMAGE_EVIDENCE)[True]
    assert set(triggers) == {"workflow_dispatch", "pull_request"}
    assert not triggers["workflow_dispatch"], "manual runs take no inputs"
    assert triggers["pull_request"] == {"paths": [".github/workflows/image-evidence.yml"]}
    concurrency = _load(IMAGE_EVIDENCE)["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}", (
        "manual runs are never cancelled"
    )


def test_image_evidence_verifies_syft_before_running_it() -> None:
    workflow = _load(IMAGE_EVIDENCE)
    assert re.fullmatch(r"\d+\.\d+\.\d+", workflow["env"]["SYFT_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", workflow["env"]["SYFT_SHA256"])
    runs = _runs(IMAGE_EVIDENCE)
    downloads = [run for run in runs if re.search(r"\b(curl|wget)\b", run)]
    assert len(downloads) == 1, "syft is the only download"
    install = downloads[0]
    assert "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/" in install
    assert not re.search(r"\|\s*(sh|bash)\b", install), "no install script is piped"
    check = install.index('echo "$SYFT_SHA256  ')
    assert "sha256sum --check --strict" in install[check:]
    assert "||" not in install, "a failed check must stop the step"
    assert check < install.index("tar -xzf")
    scans = [run for run in runs if "syft" in run and run is not install]
    assert scans and all('"$RUNNER_TEMP/syft/syft"' in run for run in scans)
    for run in scans:
        bare = run.replace('"$RUNNER_TEMP/syft/syft"', "")
        assert not re.search(r"(^|[\s;&|(])syft\s+\w", bare, re.M), "only the verified syft runs"


def test_image_evidence_builds_the_default_images_as_they_are() -> None:
    job = _load(IMAGE_EVIDENCE)["jobs"]["image"]
    assert job["strategy"].get("fail-fast") is False, "one failed image must not stop the rest"
    assert job["strategy"]["matrix"]["include"] == DEFAULT_IMAGES
    builds = [run for run in _runs(IMAGE_EVIDENCE) if re.search(r"\bdocker build\s", run)]
    assert len(builds) == 1
    assert 'docker build -f "$DOCKERFILE"' in builds[0] and '"$CONTEXT"' in builds[0]
    assert "--build-arg" not in builds[0], "default builds take no build arguments"


def test_image_evidence_records_the_base_the_dockerfiles_use() -> None:
    base = _load(IMAGE_EVIDENCE)["env"]["BASE_IMAGE"]
    root = WORKFLOWS.parents[1]
    for image in DEFAULT_IMAGES:
        dockerfile = (root / image["dockerfile"]).read_text(encoding="utf-8")
        default = rf"^(FROM|ARG PYTHON_BASE=)\s*{re.escape(base)}\s*$"
        assert re.search(default, dockerfile, re.M), f"{image['dockerfile']} is not on {base}"


def test_every_image_evidence_job_has_a_timeout() -> None:
    for name, job in _load(IMAGE_EVIDENCE)["jobs"].items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, f"job {name}: {timeout!r}"


def test_image_evidence_cannot_publish() -> None:
    content = str(_load(IMAGE_EVIDENCE)).lower()
    pushes = ("--push", "type=registry", "skopeo", "crane ", "oras ", "ghcr.io")
    assert not [word for word in PUBLISHING + pushes if word in content]
    for step in _steps(IMAGE_EVIDENCE):
        uses = step.get("uses")
        if uses:
            assert re.fullmatch(r"actions/[\w-]+@v\d+", uses), (
                f"{uses}: not a GitHub-owned action at a release tag"
            )
    for run in _runs(IMAGE_EVIDENCE):
        assert "${{" not in run, run
        assert "GITHUB_ENV" not in run and "GITHUB_PATH" not in run, run


def test_image_evidence_records_each_kind_of_evidence() -> None:
    runs = "\n".join(_runs(IMAGE_EVIDENCE))
    assert re.search(r'syft" scan "docker:image-evidence/\$IMAGE" -o "cyclonedx-json=', runs)
    assert "${source:Package}" in runs and "${source:Version}" in runs, "Debian source packages"
    assert "ls /usr/share/doc/*/copyright" in runs, "Debian copyright files"
    assert "scripts/compliance:/compliance:ro" in runs and "collect_notices.py" in runs
    assert 'collect "$purelib" bundle' in runs, "the image's own Python distributions"
    assert 'collect "$base_purelib"' in runs, "the base image's own Python, next to a venv"
    assert re.search(r'-m pip freeze --all \\\s+> "evidence/\$IMAGE-pip-freeze-all.txt"', runs)
    assert '-path "*.libs/*"' in runs, "native libraries bundled inside wheels"
    assert "{{.Id}}" in runs and "RepoDigests" in runs, "image ID and base image digest"
    assert '"$base_layers"*) ;;' in runs, "the recorded base image is the image's own base"
    assert '> "evidence/$IMAGE-base-python-pip-freeze-all.txt"' in runs
    assert "sha256sum > SHA256SUMS" in runs, "the evidence carries its own checksums"


def test_image_evidence_uploads_keep_failure_evidence() -> None:
    uploads = [
        step
        for step in _steps(IMAGE_EVIDENCE)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    step = uploads[0]
    assert step.get("if") == "always()", "a failed step still leaves its evidence"
    assert step["with"].get("if-no-files-found") == "error"
    assert step["with"].get("retention-days") == 90
    assert step["with"]["path"] == "evidence/"
