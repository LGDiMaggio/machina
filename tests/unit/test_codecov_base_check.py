"""Tests for ``scripts/codecov_base_check.py``.

The topology tests build throwaway git repositories and replay the history shapes that were
observed on the real Codecov API: PR #136 (``main`` first, then three older heads merged after it)
had its base replaced by the old fork point, while PR #134 (``main`` a direct parent of the head)
and a control PR built heads first with ``main`` merged last kept ``main``'s tip as the base.
The ``verify`` tests feed the checker payloads shaped like the live API responses for those PRs.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import urllib.error
from typing import TYPE_CHECKING, Any

import pytest
from scripts import codecov_base_check as cbc

if TYPE_CHECKING:
    from pathlib import Path

# --------------------------------------------------------------------------- git topology helpers


class Lab:
    """A throwaway repository with a fork point, an advancing ``main`` and N older heads."""

    def __init__(self, path: Path, heads: int) -> None:
        self.path = path
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "lab",
            "GIT_AUTHOR_EMAIL": "lab@example.invalid",
            "GIT_COMMITTER_NAME": "lab",
            "GIT_COMMITTER_EMAIL": "lab@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.fork = self.commit("fork point")
        self.main_tip = self.commit("main moves on")
        self.heads: list[str] = []
        for number in range(1, heads + 1):
            self.git("checkout", "-q", "--detach", self.fork)
            self.heads.append(self.commit(f"old head {number}"))
        self.git("checkout", "-q", "main")

    def git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            encoding="utf8",
            env=self.env,
            check=True,
        )
        return out.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge(self, rev: str, message: str) -> str:
        self.git("merge", "-q", "--no-ff", "-m", message, rev)
        return self.git("rev-parse", "HEAD")

    def main_first(self) -> str:
        """Start at ``main`` and merge each head after it, the way PR #136 was built."""
        self.git("checkout", "-q", "-b", "pr-main-first", "main")
        for number, head in enumerate(self.heads, start=1):
            self.merge(head, f"Merge head {number}")
        return self.git("rev-parse", "HEAD")

    def main_last(self) -> str:
        """Start at the fork point, merge the heads, then merge ``main`` last."""
        self.git("checkout", "-q", "-b", "pr-main-last", self.fork)
        for number, head in enumerate(self.heads, start=1):
            self.merge(head, f"Merge head {number}")
        return self.merge("main", "Merge main")

    def predict(self, head: str) -> cbc.BaseWalk:
        walk, _ = cbc.predict_base(str(self.path), head, "main")
        return walk


@pytest.fixture
def make_lab(tmp_path: Path) -> Any:
    def factory(heads: int) -> Lab:
        return Lab(tmp_path / f"lab{heads}", heads)

    return factory


# ------------------------------------------------------------------------------------ the walk


def test_walk_linear_branch_keeps_the_github_base() -> None:
    parents = {"c": ["b"], "b": ["base"]}
    walk = cbc.walk_codecov_base("c", parents, "base")
    assert (walk.chosen, walk.overridden, walk.outside) == ("base", False, ["base"])


def test_walk_stops_at_the_first_commit_outside_the_pr_and_overrides_the_base() -> None:
    # PR #136 in miniature: three features merged one after another onto main. The old fork
    # point (parent of the last feature) is found one level before main's tip.
    parents = {
        "m3": ["m2", "feat3"],
        "m2": ["m1", "feat2"],
        "m1": ["main", "feat1"],  # the first merge, onto main
        "feat1": ["old"],
        "feat2": ["old"],
        "feat3": ["old"],
    }
    walk = cbc.walk_codecov_base("m3", parents, "main")
    assert walk.levels == [["m3"], ["m2", "feat3"], ["m1", "feat2", "old"]]
    assert walk.outside == ["old"]
    assert (walk.chosen, walk.overridden) == ("old", True)


def test_walk_keeps_the_github_base_when_it_is_in_the_same_level() -> None:
    parents = {
        "head": ["m1", "feat2"],
        "m1": ["main", "feat1"],
        "feat1": ["old"],
        "feat2": ["old"],
    }
    walk = cbc.walk_codecov_base("head", parents, "main")
    assert walk.chosen == "main"
    assert walk.overridden is False


def test_walk_head_outside_the_pr_has_nothing_to_predict() -> None:
    walk = cbc.walk_codecov_base("base", {}, "base")
    assert walk.chosen is None


# ----------------------------------------------------------------------- history constructions


@pytest.mark.parametrize("heads", [1, 2])
def test_main_first_is_safe_with_one_or_two_heads(make_lab: Any, heads: int) -> None:
    lab = make_lab(heads)
    walk = lab.predict(lab.main_first())
    assert walk.chosen == lab.main_tip
    assert not walk.overridden


@pytest.mark.parametrize("heads", [3, 4])
def test_main_first_picks_the_old_fork_point_with_three_or_more_heads(
    make_lab: Any, heads: int
) -> None:
    lab = make_lab(heads)
    walk = lab.predict(lab.main_first())
    assert walk.chosen == lab.fork
    assert walk.overridden


@pytest.mark.parametrize("heads", [1, 2, 3, 4])
def test_main_last_is_safe_for_any_number_of_heads(make_lab: Any, heads: int) -> None:
    lab = make_lab(heads)
    walk = lab.predict(lab.main_last())
    assert walk.chosen == lab.main_tip
    assert not walk.overridden


def test_fix_commits_on_top_of_a_main_last_merge_keep_the_base(make_lab: Any) -> None:
    lab = make_lab(3)
    lab.main_last()
    for number in range(3):
        tip = lab.commit(f"fix {number}")
    assert lab.predict(tip).chosen == lab.main_tip


def test_merging_an_advanced_main_repairs_a_main_first_branch(make_lab: Any) -> None:
    lab = make_lab(3)
    tip = lab.main_first()
    assert lab.predict(tip).overridden
    lab.git("checkout", "-q", "main")
    new_main = lab.commit("main moves again")
    lab.git("checkout", "-q", "pr-main-first")
    tip = lab.merge("main", "Merge main again")
    walk, _ = cbc.predict_base(str(lab.path), tip, "main")
    assert walk.chosen == new_main
    assert not walk.overridden


def test_merging_main_into_a_main_first_branch_adds_nothing(make_lab: Any) -> None:
    lab = make_lab(3)
    tip = lab.main_first()
    lab.git("merge", "--no-ff", "-m", "Merge main", "main")
    assert lab.git("rev-parse", "HEAD") == tip  # main is already an ancestor


# --------------------------------------------------------------------------------- predict CLI


def test_predict_exit_codes(make_lab: Any, capsys: pytest.CaptureFixture[str]) -> None:
    lab = make_lab(3)
    wrong = lab.main_first()
    right = lab.main_last()
    argv = ["predict", "--repo", str(lab.path), "--base", "main"]

    assert cbc.main([*argv, "--head", right]) == 0
    assert "OK, equals the GitHub base" in capsys.readouterr().out

    assert cbc.main([*argv, "--head", wrong]) == 1
    assert "WRONG BASE" in capsys.readouterr().out

    assert cbc.main([*argv, "--head", "main"]) == 1
    assert "not ahead of the base" in capsys.readouterr().out


# ------------------------------------------------------------------------------------- verify

HEAD = "f31201a5d49bce30018d88706bc2a3266e0fc38e"
BASE = "da710bc657aaf51b3cfb8deb021e127f12af2fb7"
WRONG_BASE = "bac3027ddfb90f641b604b8e14cecc1537818bc6"
MISSING = (404, {"detail": "Sorry, we are missing a commit for that pull request."})


class FakeSources:
    """Canned answers shaped like the live API; ``codecov_paths`` overrides single endpoints."""

    def __init__(
        self,
        patch: dict[str, Any] | None,
        codecov_paths: dict[str, tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.patch = patch
        self.paths: dict[str, tuple[int, dict[str, Any]]] = {
            "pulls/7/": (200, {"base_totals": {"coverage": 89.04, "sessions": 2}}),
            "compare/?pullid=7": (200, {"base_commit": BASE, "head_commit": HEAD}),
            f"commits/{BASE}/": (200, {"state": "complete", "totals": {"coverage": 89.04}}),
            f"totals/?sha={HEAD}&flag=core": (200, {"totals": {"sessions": 1, "coverage": 79.95}}),
            f"totals/?sha={HEAD}&flag=extras": (
                200,
                {"totals": {"sessions": 1, "coverage": 89.03}},
            ),
            f"commits/{HEAD}/": (200, {"state": "complete", "totals": {"sessions": 2}}),
        }
        self.paths.update(codecov_paths or {})

    def pr(self, number: int) -> dict[str, Any]:
        return {
            "baseRefOid": BASE,
            "headRefOid": HEAD,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": "main",
        }

    def patch_check(self, sha: str) -> dict[str, Any] | None:
        return self.patch

    def codecov(self, path: str) -> tuple[int, dict[str, Any]]:
        return self.paths.get(path, MISSING)


GOOD_PATCH = {"conclusion": "success", "title": "100.00% of diff hit (target 100.00%)"}


def outcome(sources: FakeSources) -> dict[str, bool]:
    _, checks = cbc.verify_pr(sources, 7)  # type: ignore[arg-type]
    return {check.label: check.ok for check in checks}


def test_verify_passes_a_correct_integration_pr() -> None:
    assert all(outcome(FakeSources(GOOD_PATCH)).values())


def test_verify_flags_the_missing_base_even_though_the_status_is_green() -> None:
    # PR #136: Codecov replaced the base, the pull has no base_totals, the compare endpoint answers
    # 404, and codecov/patch concluded success without measuring anything.
    sources = FakeSources(
        {"conclusion": "success", "title": "No report found to compare against"},
        {
            "pulls/7/": (200, {"base_totals": None, "patch": None}),
            "compare/?pullid=7": MISSING,
        },
    )
    result = outcome(sources)
    assert result["base_totals is non-null"] is False
    assert result["compare endpoint returns a comparison"] is False
    assert result["comparison base is the GitHub base"] is False
    assert result["comparison base has a processed report"] is False
    assert result["codecov/patch reports a measurement"] is False
    assert result["codecov/patch succeeded"] is True  # the green status alone proves nothing


def test_verify_flags_a_comparison_against_another_base() -> None:
    sources = FakeSources(
        GOOD_PATCH,
        {"compare/?pullid=7": (200, {"base_commit": WRONG_BASE, "head_commit": HEAD})},
    )
    result = outcome(sources)
    assert result["comparison base is the GitHub base"] is False
    assert result["comparison base has a processed report"] is False  # no row for that commit


def test_verify_flags_a_missing_extras_upload() -> None:
    sources = FakeSources(
        GOOD_PATCH,
        {
            f"totals/?sha={HEAD}&flag=extras": (200, {"totals": {"sessions": 0, "coverage": 0}}),
            f"commits/{HEAD}/": (200, {"state": "complete", "totals": {"sessions": 1}}),
        },
    )
    result = outcome(sources)
    assert result["head has the 'extras' upload"] is False
    assert result["head report is complete with 2 sessions"] is False
    assert result["head has the 'core' upload"] is True


def test_verify_reports_an_insufficient_patch_as_measured_but_not_succeeded() -> None:
    patch = {"conclusion": "failure", "title": "99.05% of diff hit (target 100.00%)"}
    result = outcome(FakeSources(patch))
    assert result["codecov/patch reports a measurement"] is True
    assert result["codecov/patch succeeded"] is False


def test_verify_treats_a_patch_check_that_was_never_posted_as_not_ready() -> None:
    result = outcome(FakeSources(None))
    assert result["codecov/patch reports a measurement"] is False
    assert result["codecov/patch succeeded"] is False


def test_verify_accepts_a_diff_without_coverable_lines(capsys: pytest.CaptureFixture[str]) -> None:
    patch = {
        "conclusion": "success",
        "title": "Coverage not affected when comparing 598bfeb...2228093",
    }
    _, checks = cbc.verify_pr(FakeSources(patch), 7)  # type: ignore[arg-type]
    assert all(check.ok for check in checks)
    assert "holds no coverable lines" in checks[-2].detail


def test_verify_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cbc, "Sources", lambda slug: FakeSources(GOOD_PATCH))
    assert cbc.main(["verify", "--pr", "7"]) == 0
    assert "all checks passed" in capsys.readouterr().out

    monkeypatch.setattr(cbc, "Sources", lambda slug: FakeSources(None))
    assert cbc.main(["verify", "--pr", "7"]) == 1
    assert "do not call this PR ready" in capsys.readouterr().out


# -------------------------------------------------------------------------------- API plumbing


def http_error(code: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.codecov.io/x",
        code,
        "err",
        {},
        io.BytesIO(json.dumps(body).encode()),  # type: ignore[arg-type]
    )


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_codecov_retries_transient_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Any] = [http_error(503, {}), FakeResponse(b'{"ok": true}')]

    def fake_urlopen(url: str) -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cbc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cbc.time, "sleep", lambda seconds: None)
    assert cbc.Sources("o/r").codecov("pulls/1/") == (200, {"ok": True})


def test_codecov_returns_a_client_error_with_its_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(url: str) -> Any:
        raise http_error(404, {"detail": "missing"})

    monkeypatch.setattr(cbc.urllib.request, "urlopen", fake_urlopen)
    assert cbc.Sources("o/r").codecov("compare/?pullid=1") == (404, {"detail": "missing"})
