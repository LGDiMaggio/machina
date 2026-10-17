"""Pin the safety and completeness rules of the GitHub Actions workflows.

The weekly audit (.github/workflows/audit.yml) only runs on a schedule, on
demand, or when its own files change, so a pull request that breaks one of
its rules would otherwise go unnoticed until the next Monday.
"""

from __future__ import annotations

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
