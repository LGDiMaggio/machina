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
