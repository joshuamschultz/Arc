"""SPEC-022 Acceptance Criterion 21 — arcagent core LOC budget unaffected.

The spec scopes the entire change to arcui + arcgateway. arcagent must
not be touched. Verify by `git diff --stat` against main: zero files
under packages/arcagent/.

Tolerated when:
  - The repo is checked out at a commit not based on main (we silently
    pass — the assertion is a guardrail, not a CI tripwire on detached
    HEAD).

Skipped when:
  - `git` is not on PATH (rare; this test ships in a Python-only env)
  - Working tree is dirty under arcagent/ AND is the only signal we have
    (we report the dirty paths instead of failing flatly).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _git(*args: str) -> tuple[int, str]:
    git = shutil.which("git")
    if git is None:
        return 127, "git not on PATH"
    proc = subprocess.run(
        [git, *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


class TestArcagentUnmodified:
    def test_no_committed_changes_to_arcagent_vs_main(self) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")

        # If the branch isn't ahead of main, there are no committed changes
        # by definition. main..HEAD returns nothing.
        rc, out = _git("diff", "--stat", "main..HEAD", "--", "packages/arcagent/")
        if rc != 0:
            pytest.skip(f"git diff failed (likely no main ref): {out.strip()}")
        assert out.strip() == "", (
            f"arcagent has committed changes vs main:\n{out}"
        )

    def test_no_uncommitted_changes_under_arcagent(self) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")

        rc, out = _git("status", "--short", "--", "packages/arcagent/")
        if rc != 0:
            pytest.skip(f"git status failed: {out.strip()}")
        assert out.strip() == "", (
            f"arcagent has uncommitted changes (SPEC-022 must not touch core):\n{out}"
        )
