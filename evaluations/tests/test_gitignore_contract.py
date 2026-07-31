"""Contract tests for the SPEC-060 `.gitignore` block — COMP-014 / REQ-195.

Every assertion here runs the real `git check-ignore`, never a re-implementation
of git's pattern matching. `--no-index` is passed throughout so the result is
pure pattern matching: without it git reports an already-tracked path as *not*
ignored no matter what the patterns say, which would make the negative
assertions below pass vacuously.

Two failure modes are under test, and they pull in opposite directions:

1. **Too narrow** — an artifact path escapes into the repo. The harness writes a
   third-party dataset, a throwaway agent workspace per question, traces, an
   audit chain, generated agent configs and a results ledger; none of that is
   repo content and all of it is trackable today.
2. **Too broad** — a pattern swallows something that must stay trackable. This
   is the more expensive mistake: git cannot re-include a file beneath an
   ignored directory, so a `!` negation cannot rescue harness source that fell
   under one, and an over-broad rule in this same file has silently hidden a
   whole documentation tree before. The anchoring of `/capabilities/` is the
   live example — unanchored it would hide
   `packages/arcagent/src/arcagent/builtins/capabilities/`.

The paths exercised below need not exist on disk. A `.gitignore` is a contract
about paths, and `check-ignore --no-index` evaluates it as one, so the artifact
paths are asserted before the harness ever creates them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / ".gitignore"

# The exact patterns COMP-014 ships. Duplicated here deliberately: the list is
# the requirement, and a test that read the patterns out of the file it is
# checking could not detect one going missing.
SPEC_060_PATTERNS = (
    "/evaluations/longmemeval/data/",
    "/evaluations/runs/",
    "/evaluations/results/",
    "/evaluations/**/traces/",
    "/evaluations/**/.audit/",
    "/evaluations/**/arcagent.toml",
    "/evaluations/**/arcllm.toml",
    "/evaluations/**/arcrun.toml",
    "/workspace/",
    "/traces/",
    "/.audit/",
    "/capabilities/",
    "/arcagent.toml",
    "/arcllm.toml",
    "/arcrun.toml",
)

# Tracked paths a pre-existing, non-SPEC-060 rule already hides. Empty, and the
# second assertion below keeps it that way: an entry added here fails the moment
# the underlying debt is paid, so the set cannot quietly outlive its reason.
KNOWN_IGNORED_TRACKED: frozenset[str] = frozenset()

# Resolved rather than spelled "git" so the call carries an absolute executable
# path (ruff S607) and a missing git fails loudly instead of at the shell.
GIT = shutil.which("git")


def _run_git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    assert GIT is not None, "git is required to verify the .gitignore contract"
    return subprocess.run(
        [GIT, *args],
        cwd=REPO_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _is_ignored(path: str) -> bool:
    """Ask git, not a regex, whether `path` is ignored. Exit 0 ignored, 1 not."""
    result = _run_git("check-ignore", "--no-index", "-q", "--", path)
    assert result.returncode in (0, 1), (
        f"git check-ignore failed for {path!r}: rc={result.returncode} {result.stderr.strip()}"
    )
    return result.returncode == 0


# ------------------------------------------------------------------ patterns present


@pytest.mark.parametrize("pattern", SPEC_060_PATTERNS)
def test_the_required_pattern_is_present(pattern: str) -> None:
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()

    assert pattern in lines, f"REQ-195 pattern {pattern!r} is missing from .gitignore"


def test_no_blanket_toml_glob_was_used() -> None:
    """A `*.toml` rule would hide every pyproject.toml and blueprint.toml in the repo."""
    lines = [line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()]

    assert not any(line.endswith("*.toml") and not line.startswith("!") for line in lines)


# ------------------------------------------------------------------ artifacts ignored


@pytest.mark.parametrize(
    "artifact",
    [
        # Dataset (COMP-003) — a third-party download, never repo content.
        "evaluations/longmemeval/data/longmemeval_oracle.json",
        "evaluations/longmemeval/data/longmemeval_s_cleaned.json",
        # Per-question throwaway workspaces (COMP-019).
        "evaluations/runs/lme-q0001/context.md",
        "evaluations/runs/lme-q0001/memory/daily-log/2023-05-20.md",
        # Results ledger and manifest (COMP-018, COMP-016).
        "evaluations/results/oracle.jsonl",
        "evaluations/results/run_manifest.json",
        # Generated agent configs under a run dir, by individual name.
        "evaluations/runs/lme-q0001/arcagent.toml",
        "evaluations/runs/lme-q0001/arcllm.toml",
        "evaluations/runs/lme-q0001/arcrun.toml",
        # Traces and the audit chain written inside a run workspace.
        "evaluations/runs/lme-q0001/traces/trace-0001.jsonl",
        "evaluations/runs/lme-q0001/.audit/chain.jsonl",
        # Repo-root guards: ArcAgent(cfg) without an absolute config_path
        # resolves all four of these against the process CWD, the repo root.
        "workspace/lme-q0001/context.md",
        "traces/trace-0001.jsonl",
        ".audit/chain.jsonl",
        "capabilities/leaked_tool.py",
        "arcagent.toml",
        "arcllm.toml",
        "arcrun.toml",
    ],
)
def test_the_harness_artifact_is_ignored(artifact: str) -> None:
    assert _is_ignored(artifact), f"{artifact} would be committable"


# -------------------------------------------------------------- source stays tracked


@pytest.mark.parametrize(
    "source",
    [
        # Harness source. git cannot re-include a file beneath an ignored
        # directory, so if any of these were ignored the damage is permanent
        # and no `!` negation could undo it.
        "evaluations/__init__.py",
        "evaluations/README.md",
        "evaluations/ingest/__init__.py",
        "evaluations/ingest/types.py",
        "evaluations/ingest/chunker.py",
        "evaluations/ingest/driver.py",
        "evaluations/ingest/lifecycle.py",
        "evaluations/longmemeval/__init__.py",
        "evaluations/longmemeval/runner.py",
        "evaluations/longmemeval/hygiene.py",
        "evaluations/longmemeval/cli.py",
        "evaluations/tests/__init__.py",
        "evaluations/tests/test_gitignore_contract.py",
        # The checked-in config templates the individually-named TOML patterns
        # exist to spare.
        "evaluations/longmemeval/config/arcagent.toml.example",
        "evaluations/longmemeval/config/arcllm.toml.example",
        "evaluations/longmemeval/config/arcrun.toml.example",
        # Real tracked files that an unanchored root guard would hide. These are
        # the anchoring regression test, not hypotheticals.
        "packages/arcagent/src/arcagent/builtins/capabilities/bash.py",
        "blueprints/sales-exec-assistant/capabilities/crm.py",
    ],
)
def test_the_source_path_is_not_ignored(source: str) -> None:
    assert not _is_ignored(source), f"{source} is hidden from git and cannot be re-included"


# ----------------------------------------------------------- nothing tracked was lost


def test_no_tracked_file_beyond_known_debt_is_ignored() -> None:
    """The sweep the hidden-ADR incident earned: every tracked path, not a sample.

    Deliberately not scoped to SPEC-060's own patterns. An over-broad rule is
    over-broad whoever adds it, and this is the only place in the repo that
    checks.
    """
    tracked = _run_git("ls-files")
    assert tracked.returncode == 0, tracked.stderr

    matched = _run_git("check-ignore", "--stdin", "--no-index", "-v", stdin=tracked.stdout)
    assert matched.returncode in (0, 1), matched.stderr

    hidden: dict[str, str] = {}
    for line in matched.stdout.splitlines():
        source, _, remainder = line.partition(":")
        _, _, remainder = remainder.partition(":")
        pattern, _, path = remainder.partition("\t")
        # `-v` reports the last matching pattern, negations included; a leading
        # `!` means the path was rescued and is not ignored.
        if not pattern.startswith("!"):
            hidden[path] = f"{source}:{pattern}"

    new = {path: rule for path, rule in hidden.items() if path not in KNOWN_IGNORED_TRACKED}
    assert new == {}, "these tracked files are now hidden from git: " + str(new)

    fixed = KNOWN_IGNORED_TRACKED - hidden.keys()
    assert fixed == set(), f"debt paid — drop {fixed} from KNOWN_IGNORED_TRACKED"
