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
