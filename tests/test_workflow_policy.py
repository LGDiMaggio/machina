"""Pin the safety and completeness rules of the GitHub Actions workflows.

The weekly audit (.github/workflows/audit.yml) and the weekly litellm check
(.github/workflows/litellm-compat.yml) only run on a schedule, on demand, or
when their own files change, so a pull request that breaks one of their rules
would otherwise go unnoticed until the next Monday.
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
