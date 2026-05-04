"""SPEC-022 Acceptance Criterion 21 — arcagent core LOC budget effectively unaffected.

The spec originally scoped the entire change to arcui + arcgateway. The
post-rehearsal "live agents not showing online" debug session forced one
exception: ``arcagent.modules.ui_reporter`` was the only module on the
critical path that *had* to change for `agent_name`-keyed roster overlay
to function. ``_runtime.configure`` previously bound state without ever
opening a WebSocket transport (the transport-start lived in an unused
``UIReporterModule.startup`` hook). Without this fix the arcui
agent_registry would always be empty regardless of how many live agents
existed.

SPEC-023 added one more documented exception: ``arcagent.core.agent``'s
``_ensure_model`` was passing ``self._workspace`` (the workspace dir)
to ``JSONLTraceStore``, which then wrote traces under
``<workspace>/traces/`` — inside the agent's tool sandbox, violating
NIST AU-9 (traces must live outside the sandbox) and putting them at
a path arcui's federated trace store does not read. The fix changed
that one argument to ``self._workspace.parent`` so traces live at
``<agent_root>/traces/`` where ``arcllm.JSONLTraceStore`` documents
they belong. Documented in SDD §13.5.

This test now enforces "no changes to arcagent except the explicitly
allowed files" so a future PR can't quietly regress the core LOC budget.

Tolerated when:
  - Branch not based on main (silently passes — guardrail not tripwire).
Skipped when:
  - `git` is not on PATH.
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


# Files explicitly allowed to change as part of SPEC-022 / SPEC-023.
# Anything else under packages/arcagent/ flips this test red — the core
# LOC budget is off-limits except for documented exceptions.
_ALLOWED_ARCAGENT_PATHS = (
    # SPEC-022 ui_reporter connect-path fix.
    "packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py",
    "packages/arcagent/src/arcagent/modules/ui_reporter/_runtime.py",
    # SPEC-023 NIST AU-9 trace path fix (one-line change in _ensure_model).
    "packages/arcagent/src/arcagent/core/agent.py",
)


def _filter_allowed(diff_lines: list[str]) -> list[str]:
    """Return only diff-stat lines that aren't the summary footer
    (`N files changed, ...`) and that name a file NOT on the allowlist."""
    out: list[str] = []
    for line in diff_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if "files changed" in stripped or "file changed" in stripped:
            # Diff-stat summary footer; meaningless once the per-file rows
            # are filtered. Drop it.
            continue
        if "|" not in line:
            out.append(line)
            continue
        path = line.split("|", 1)[0].strip()
        # diff --stat prints abbreviated paths like ".../arcagent/modules/...".
        # Suffix-match for robustness.
        allowed_suffixes = tuple(
            p.split("/", 1)[-1] for p in _ALLOWED_ARCAGENT_PATHS
        )
        if any(path.endswith(s) or s.endswith(path.lstrip(".").lstrip("/"))
               for s in allowed_suffixes):
            continue
        out.append(line)
    return out


class TestArcagentUnmodified:
    def test_only_allowed_arcagent_changes_vs_main(self) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")

        rc, out = _git("diff", "--stat", "main..HEAD", "--", "packages/arcagent/")
        if rc != 0:
            pytest.skip(f"git diff failed (likely no main ref): {out.strip()}")
        if not out.strip():
            return  # nothing changed — happy path

        unexpected = _filter_allowed(out.strip().splitlines())
        assert not unexpected, (
            "arcagent has committed changes outside the SPEC-022 ui_reporter "
            "exception list. Allowed: ui_reporter/__init__.py, "
            "ui_reporter/_runtime.py.\nUnexpected:\n" + "\n".join(unexpected)
        )

    def test_no_uncommitted_changes_under_arcagent(self) -> None:
        """Reject uncommitted edits to arcagent files outside the allow list.

        Files in ``_ALLOWED_ARCAGENT_PATHS`` are exempt — they document
        the legitimate exceptions where SPEC-022/SPEC-023 had to touch
        arcagent core. All other uncommitted changes flip the test red.
        """
        if shutil.which("git") is None:
            pytest.skip("git not on PATH")

        rc, out = _git("status", "--short", "--", "packages/arcagent/")
        if rc != 0:
            pytest.skip(f"git status failed: {out.strip()}")
        if not out.strip():
            return  # nothing changed — happy path

        # NB: don't `out.strip()` before splitting — it eats the leading
        # space of the first line's status prefix (`git status --short`
        # uses `XY ` two-char + space format, the first char is often a
        # space for unstaged-only changes).
        unexpected: list[str] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            # `git status --short` lines are `XY filename`. Skip the
            # three-char prefix to extract the path.
            path = (line[3:] if len(line) > 3 else line).split(" -> ", 1)[-1]
            if path in _ALLOWED_ARCAGENT_PATHS:
                continue
            unexpected.append(line)

        assert not unexpected, (
            "arcagent has uncommitted changes outside the allow list:\n"
            + "\n".join(unexpected)
        )
