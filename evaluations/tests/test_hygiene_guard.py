"""RepoHygieneGuard tests — COMP-014 / REQ-195, REQ-196.

Every assertion runs against a throwaway git repository built in `tmp_path`, not
against the Arc repo. The guard's whole job is to fail when a pattern is
missing, and that case cannot be exercised in a repo whose `.gitignore` is
correct — so the test owns the repo it checks.

The fixture pins `core.excludesFile` to an empty file. Without it a developer's
global gitignore could match `/runs/` or `/results/` and the removal test would
pass for the wrong reason.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from evaluations.longmemeval.hygiene import RepoHygieneError, RepoHygieneGuard

GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="git is required to exercise the guard")

# What COMP-014 ships, reduced to the two rules this guard checks at phase start.
RUN_DIR_PATTERN = "/runs/"
RESULTS_PATTERN = "/results/"


def _write_gitignore(repo: Path, patterns: list[str]) -> None:
    repo.joinpath(".gitignore").write_text("\n".join(patterns) + "\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, empty git repo with both patterns present. No commit needed."""
    assert GIT is not None
    subprocess.run([GIT, "init", "-q"], cwd=tmp_path, check=True, capture_output=True)

    empty_excludes = tmp_path / "empty-excludes"
    empty_excludes.write_text("", encoding="utf-8")
    subprocess.run(
        [GIT, "config", "core.excludesFile", str(empty_excludes)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    _write_gitignore(tmp_path, [RUN_DIR_PATTERN, RESULTS_PATTERN])
    return tmp_path


def _guard(repo: Path) -> RepoHygieneGuard:
    return RepoHygieneGuard(
        run_dir=repo / "runs" / "lme-q0001",
        results_path=repo / "results" / "oracle.jsonl",
        repo_root=repo,
    )


def test_both_paths_ignored_passes(repo: Path) -> None:
    _guard(repo).check()


def test_removing_the_run_dir_pattern_aborts_the_phase(repo: Path) -> None:
    _write_gitignore(repo, [RESULTS_PATTERN])

    with pytest.raises(RepoHygieneError) as excinfo:
        _guard(repo).check()

    assert str(repo / "runs" / "lme-q0001") in str(excinfo.value)


def test_removing_the_results_pattern_aborts_the_phase(repo: Path) -> None:
    _write_gitignore(repo, [RUN_DIR_PATTERN])

    with pytest.raises(RepoHygieneError) as excinfo:
        _guard(repo).check()

    assert str(repo / "results" / "oracle.jsonl") in str(excinfo.value)


def test_the_guard_writes_nothing_before_it_aborts(repo: Path) -> None:
    """REQ-196: the abort has to land before the harness creates an artifact."""
    _write_gitignore(repo, [])
    run_dir = repo / "runs" / "lme-q0001"
    results_path = repo / "results" / "oracle.jsonl"

    with pytest.raises(RepoHygieneError):
        _guard(repo).check()

    assert not run_dir.exists()
    assert not results_path.exists()
    assert not results_path.parent.exists()


def test_a_tracked_artifact_path_is_reported_as_unignored(repo: Path) -> None:
    """git reports a tracked path as not ignored, and that is a leak worth failing on."""
    assert GIT is not None
    results_path = repo / "results" / "oracle.jsonl"
    results_path.parent.mkdir(parents=True)
    results_path.write_text("{}\n", encoding="utf-8")
    subprocess.run(
        [GIT, "add", "-f", str(results_path)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(RepoHygieneError) as excinfo:
        _guard(repo).check()

    assert str(results_path) in str(excinfo.value)


def test_a_non_git_directory_raises_rather_than_passing(tmp_path: Path) -> None:
    """A git failure must never be read as 'ignored' — that would disable the guard."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    with pytest.raises(RepoHygieneError) as excinfo:
        RepoHygieneGuard(
            run_dir=plain / "runs" / "lme-q0001",
            results_path=plain / "results" / "oracle.jsonl",
            repo_root=plain,
        ).check()

    message = str(excinfo.value)
    assert "git check-ignore" in message
    assert "is not ignored" not in message, "a git failure must not masquerade as a pattern miss"
