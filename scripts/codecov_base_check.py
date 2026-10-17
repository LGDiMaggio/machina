"""Check that Codecov compares an integration PR against the right base.

Codecov does not use GitHub's ``base.sha``. It re-derives the PR base by walking the head's ancestry
breadth-first through the PR's own commits. The walk stops at the first level that holds a commit
outside the PR, and if GitHub's base is not among those commits, Codecov replaces the base with the
first one found (``GitHub.get_pull_request`` in codecov/umbrella, ``libs/shared/shared/torngit``).
A branch that merges several older branches *after* ``main`` can therefore get an old fork point as
its base. When that commit has no coverage report, the PR has no comparison: the pulls API returns
``base_totals: null``, the compare endpoint answers 404, and ``codecov/patch`` passes without
measuring anything ("No report found to compare against").

Two subcommands, both read-only (no tokens, nothing is written)::

    python scripts/codecov_base_check.py predict --head HEAD [--base origin/main] [--repo .]
    python scripts/codecov_base_check.py verify --pr 123 [--repo-slug LGDiMaggio/machina]

``predict`` runs before a push, from local git only (run ``git fetch origin main`` first). It prints
the base Codecov will choose and exits 1 when that is not the GitHub base.

``verify`` runs after CI has finished on the PR head. It needs the ``gh`` CLI and exits 1 unless
every readiness check passes: the comparison base is the GitHub base and has a processed report,
``base_totals`` is non-null, the compare endpoint returns a comparison, both the ``core`` and
``extras`` uploads are in the head report, and ``codecov/patch`` holds a measurement and succeeded.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SLUG = "LGDiMaggio/machina"
CODECOV_API = "https://api.codecov.io/api/v2/github/{owner}/repos/{name}"
FLAGS = ("core", "extras")  # one coverage upload per flag, see .github/workflows/ci.yml
# GitHub lists at most this many commits of a PR; past it Codecov's walk cannot be reproduced.
GITHUB_PR_COMMIT_LIMIT = 250
# A real patch result: a measured percentage, or a comparison that names its base commit (the diff
# held no coverable lines). "No report found to compare against" matches neither.
MEASURED_PATCH = re.compile(
    r"\d+(\.\d+)?% of diff hit|Coverage not affected when comparing \w+\.\.\.\w+"
)


# --------------------------------------------------------------------------------------- predict


@dataclass
class BaseWalk:
    """Outcome of Codecov's base walk for one head."""

    chosen: str | None  # the base Codecov will use; None when the head is not ahead of the base
    overridden: bool  # True when Codecov replaces the GitHub base
    levels: list[list[str]] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)  # first non-PR commits the walk found
    pr_commits: int = 0


def walk_codecov_base(head: str, parents: dict[str, list[str]], github_base: str) -> BaseWalk:
    """Reproduce Codecov's base selection.

    ``parents`` maps every commit of the PR to its parents (commits outside the PR are not keys).
    """
    in_pr = set(parents)
    if head not in in_pr:
        return BaseWalk(chosen=None, overridden=False, levels=[[head]], pr_commits=len(in_pr))
    current = [head]
    levels: list[list[str]] = []
    while current and all(sha in in_pr for sha in current):
        levels.append(list(current))
        current = [parent for sha in current for parent in parents[sha]]
    levels.append(list(current))
    outside = [sha for sha in current if sha not in in_pr]
    overridden = bool(outside) and github_base not in outside
    chosen = outside[0] if overridden else github_base
    return BaseWalk(chosen, overridden, levels, outside, len(in_pr))


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf8", check=False
    )
    if out.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def predict_base(repo: str, head: str, base: str) -> tuple[BaseWalk, str]:
    """Walk the history of ``head`` the way Codecov will; returns the walk and the full base SHA."""
    head_sha = _git(repo, "rev-parse", head).strip()
    base_sha = _git(repo, "rev-parse", base).strip()
    # GitHub lists a PR's commits as those reachable from the head and not from the base.
    parents: dict[str, list[str]] = {}
    for line in _git(repo, "rev-list", "--parents", f"{base_sha}..{head_sha}").splitlines():
        sha, *rest = line.split()
        parents[sha] = rest
    return walk_codecov_base(head_sha, parents, base_sha), base_sha


def cmd_predict(args: argparse.Namespace) -> int:
    walk, base = predict_base(args.repo, args.head, args.base)
    print(f"PR commits: {walk.pr_commits}   GitHub base ({args.base}): {base[:8]}")
    for number, level in enumerate(walk.levels):
        print(f"  level {number}: {[sha[:8] for sha in level]}")
    print(f"  first commits outside the PR: {[sha[:8] for sha in walk.outside]}")
    if walk.chosen is None:
        print("RESULT: the head is not ahead of the base; nothing to predict")
        return 1
    if walk.pr_commits > GITHUB_PR_COMMIT_LIMIT:
        print(
            f"WARNING: GitHub lists at most {GITHUB_PR_COMMIT_LIMIT} PR commits; verify after CI"
        )
    if walk.overridden:
        print(
            f"RESULT: Codecov base = {walk.chosen[:8]}: WRONG BASE; rebuild the history (main last)"
        )
        return 1
    print(f"RESULT: Codecov base = {walk.chosen[:8]}: OK, equals the GitHub base")
    return 0


# ---------------------------------------------------------------------------------------- verify


class Sources:
    """Network access for ``verify``: GitHub through the ``gh`` CLI, Codecov's public API."""

    def __init__(self, slug: str, attempts: int = 4) -> None:
        self.slug = slug
        self.attempts = attempts
        owner, name = slug.split("/")
        self.codecov_root = CODECOV_API.format(owner=owner, name=name)

    def _gh(self, *args: str) -> Any:
        out = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf8", check=False
        )
        if out.returncode:
            raise RuntimeError(f"gh {' '.join(args)}: {out.stderr.strip()}")
        return json.loads(out.stdout)

    def pr(self, number: int) -> dict[str, Any]:
        fields = "baseRefOid,headRefOid,state,isDraft,baseRefName"
        return dict(self._gh("pr", "view", str(number), "--repo", self.slug, "--json", fields))

    def patch_check(self, sha: str) -> dict[str, Any] | None:
        """The ``codecov/patch`` check run on a commit, or None when Codecov posted none."""
        jq = '[.check_runs[]|select(.app.slug=="codecov" and .name=="codecov/patch")'
        jq += "|{conclusion,title:.output.title}]"
        runs = self._gh("api", f"repos/{self.slug}/commits/{sha}/check-runs", "--jq", jq)
        return dict(runs[0]) if runs else None

    def codecov(self, path: str) -> tuple[int, dict[str, Any]]:
        """GET a Codecov API path; 5xx answers are transient and retried."""
        url = f"{self.codecov_root}/{path}"
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(url) as response:
                    return response.status, dict(json.load(response))
            except urllib.error.HTTPError as error:
                if error.code >= 500 and attempt < self.attempts - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                try:
                    return error.code, dict(json.load(error))
                except ValueError:
                    return error.code, {}
        return 599, {}


@dataclass
class Check:
    label: str
    ok: bool
    detail: str


def verify_pr(sources: Sources, number: int) -> tuple[str, list[Check]]:
    """Run the readiness checks for PR ``number``; returns a header line and the checks."""
    pr = sources.pr(number)
    github_base, head = pr["baseRefOid"], pr["headRefOid"]
    state = "draft" if pr["isDraft"] else str(pr["state"]).lower()
    header = f"PR #{number}: head {head[:8]}, GitHub base {github_base[:8]} ({pr['baseRefName']}), {state}"
    checks: list[Check] = []

    status, pull = sources.codecov(f"pulls/{number}/")
    base_totals = pull.get("base_totals") if status == 200 else None
    detail = f"HTTP {status}, coverage={(base_totals or {}).get('coverage')}"
    checks.append(Check("base_totals is non-null", bool(base_totals), detail))

    status, compare = sources.codecov(f"compare/?pullid={number}")
    base_commit = compare.get("base_commit") if status == 200 else None
    head_commit = compare.get("head_commit") if status == 200 else None
    detail = f"HTTP {status}" + ("" if status == 200 else f" {compare.get('detail', '')}")
    checks.append(Check("compare endpoint returns a comparison", bool(base_commit), detail))
    checks.append(
        Check(
            "comparison head is the PR head",
            head_commit == head,
            f"codecov head={str(head_commit)[:8]}",
        )
    )
    detail = f"codecov base={str(base_commit)[:8]} GitHub base={github_base[:8]}"
    checks.append(Check("comparison base is the GitHub base", base_commit == github_base, detail))

    if base_commit:
        status, base_report = sources.codecov(f"commits/{base_commit}/")
        processed = (
            status == 200
            and base_report.get("state") == "complete"
            and bool(base_report.get("totals"))
        )
        detail = f"HTTP {status}, state={base_report.get('state')}"
        checks.append(Check("comparison base has a processed report", processed, detail))
    else:
        checks.append(Check("comparison base has a processed report", False, "no comparison base"))

    for flag in FLAGS:
        status, totals = sources.codecov(f"totals/?sha={head}&flag={flag}")
        sessions = (totals.get("totals") or {}).get("sessions", 0) if status == 200 else 0
        checks.append(
            Check(
                f"head has the '{flag}' upload",
                sessions >= 1,
                f"HTTP {status}, sessions={sessions}",
            )
        )
    status, head_report = sources.codecov(f"commits/{head}/")
    sessions = (head_report.get("totals") or {}).get("sessions") if status == 200 else None
    complete = status == 200 and head_report.get("state") == "complete" and sessions == len(FLAGS)
    detail = f"HTTP {status}, state={head_report.get('state')}, sessions={sessions}"
    checks.append(Check(f"head report is complete with {len(FLAGS)} sessions", complete, detail))

    patch = sources.patch_check(head)
    title = (patch or {}).get("title") or ""
    if patch is None:
        detail = (
            "no codecov/patch check on the head (CI unfinished, or Codecov has not processed it)"
        )
    else:
        detail = f"{patch['conclusion']}: {title}"
        if title.startswith("Coverage not affected"):
            detail += " (the diff holds no coverable lines)"
    checks.append(
        Check("codecov/patch reports a measurement", bool(MEASURED_PATCH.search(title)), detail)
    )
    conclusion = (patch or {}).get("conclusion")
    checks.append(
        Check("codecov/patch succeeded", conclusion == "success", f"conclusion={conclusion}")
    )
    return header, checks


def cmd_verify(args: argparse.Namespace) -> int:
    header, checks = verify_pr(Sources(args.repo_slug), args.pr)
    print(header)
    width = max(len(check.label) for check in checks)
    for check in checks:
        print(f"  [{'PASS' if check.ok else 'FAIL'}] {check.label.ljust(width)}  {check.detail}")
    failed = [check for check in checks if not check.ok]
    if failed:
        print(f"RESULT: {len(failed)} check(s) failed; do not call this PR ready")
        return 1
    print("RESULT: all checks passed")
    return 0


# ------------------------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    predict = commands.add_parser(
        "predict", help="predict the base Codecov will pick, from local git"
    )
    predict.add_argument("--head", default="HEAD")
    predict.add_argument("--base", default="origin/main")
    predict.add_argument("--repo", default=".")
    predict.set_defaults(func=cmd_predict)
    verify = commands.add_parser(
        "verify", help="check the Codecov API and GitHub for a PR after CI"
    )
    verify.add_argument("--pr", type=int, required=True)
    verify.add_argument("--repo-slug", default=DEFAULT_SLUG)
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
