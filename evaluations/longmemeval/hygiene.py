"""RepoHygieneGuard (COMP-014) — the runtime half of the `.gitignore` contract.

The patterns shipped in `.gitignore` (REQ-195) are enforced by nothing at run
time on their own; a rule edited away, or a run directory pointed somewhere the
patterns do not cover, leaks a third-party dataset, an agent workspace, a trace
tree and an audit chain into the repo. REQ-196 closes that: the phase asks git
itself whether both artifact paths are ignored, and refuses to start otherwise.

The check runs before the first artifact is written, so the failure costs
nothing to recover from — the harness has created nothing yet.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class RepoHygieneError(Exception):
    """An artifact path is not gitignored, or git could not be asked."""


class RepoHygieneGuard:
    """Assert that the run directory and the results file are both gitignored.

    `git check-ignore` is deliberately run without `--no-index`, so an artifact
    path that is *already tracked* reports as not ignored and fails the phase.
    That is the outcome we want: a committed artifact is the leak the patterns
    exist to prevent, and a passing pattern does not undo it.
    """

    def __init__(
        self,
        *,
        run_dir: Path,
        results_path: Path,
        repo_root: Path = REPO_ROOT,
    ) -> None:
        self._run_dir = run_dir
        self._results_path = results_path
        self._repo_root = repo_root

    def check(self) -> None:
        """Raise `RepoHygieneError` unless git ignores both paths. Writes nothing."""
        for path in (self._run_dir, self._results_path):
            self._assert_ignored(path)

    def _assert_ignored(self, path: Path) -> None:
        git = shutil.which("git")
        if git is None:
            raise RepoHygieneError(
                "git check-ignore cannot run: git is not on PATH, so the .gitignore "
                "contract cannot be verified."
            )

        # `--` terminates the option list, so a path can never be read as a flag.
        result = subprocess.run(  # noqa: S603  # reason: fixed argv, absolute git path, no shell
            [git, "check-ignore", "-q", "--", str(path)],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

        # 0 = ignored, 1 = not ignored, anything else = git itself failed.
        # Treating that third case as success would silently disable the guard
        # exactly when the repo state is most suspect.
        if result.returncode == 0:
            return
        if result.returncode == 1:
            raise RepoHygieneError(
                f"{path} is not ignored by git; add the pattern to .gitignore before "
                "running a phase (REQ-195, REQ-196)."
            )
        raise RepoHygieneError(
            f"git check-ignore failed for {path} in {self._repo_root} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
