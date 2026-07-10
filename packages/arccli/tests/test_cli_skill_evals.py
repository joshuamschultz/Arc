"""RED tests for `arc skill evals` subcommands (SPEC-054 T-731, COMP-009).

Pins the CLI surface for golden-suite management (REQ-119) and provenance
stripping on human edit (REQ-111):

* ``arc skill evals <skill_path>`` — static AST listing (never executes eval code).
* ``arc skill evals edit <skill_path> <file> [--force]`` — $VISUAL/$EDITOR on a
  temp copy, validate-on-save, atomic commit, git-style abort semantics.
* ``arc skill evals regen <skill_path> [--yes]`` — unified-diff preview + confirm;
  bare CLI has no LLM invoker, so post-confirm regen errors with "agent context".

Skill resolution matches ``arc skill validate``: a path to the skill folder.
Concurrent-edit guard marker: ``<skill>/evals/.improver.lock`` (lock.py naming).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"

_GENERATED = "test_golden_generated.py"
_HUMAN = "test_human.py"

_MACHINE_DOCSTRING = (
    '"""@generated golden anchors — machine-authored by arcskill.improver.suitegen."""'
)

_VALID_SKILL_MD = """\
---
name: evals-skill
version: 1.0.0
description: A code-kind skill with a golden eval suite.
triggers: [test, demo]
tools: [bash]
---

## Resources

(auto)

## Contract

Inputs you must have:
- something

Outputs the agent must produce:
- something

## Knowledge

Background.

## Steps

1. Do thing.

## Anti Patterns

- **Don't** skip steps.

## Examples

```python
example()
```

## Validation

- It worked.
"""


def _machine_module(case_names: list[str]) -> str:
    """A machine-authored eval module: @generated docstring + real (non-placeholder) cases."""
    funcs = "\n\n".join(f"def {name}():\n    assert len('abc') == 3" for name in case_names)
    return f"{_MACHINE_DOCSTRING}\n\n{funcs}\n"


def _human_module(case_names: list[str]) -> str:
    funcs = "\n\n".join(
        f"def {name}():\n    assert sorted([2, 1]) == [1, 2]" for name in case_names
    )
    return f'"""Human-authored eval cases."""\n\n{funcs}\n'


def _make_skill(
    root: Path,
    *,
    machine_cases: list[str] | None = None,
    human_cases: list[str] | None = None,
) -> Path:
    """Build a code-kind skill folder with a golden suite + harness manifest."""
    skill_dir = root / "evals-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_VALID_SKILL_MD)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "tool.py").write_text("def run(x):\n    return x + 1\n")
    evals = skill_dir / "evals"
    evals.mkdir()

    manifest_files: dict[str, dict[str, str]] = {}
    if machine_cases is not None:
        content = _machine_module(machine_cases)
        (evals / _GENERATED).write_text(content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest_files[_GENERATED] = {"sha256": digest}
    if human_cases is not None:
        (evals / _HUMAN).write_text(_human_module(human_cases))
    (evals / ".manifest.json").write_text(json.dumps({"files": manifest_files}, indent=2))
    return skill_dir


def _write_editor(path: Path, body: str) -> Path:
    """Write an executable fake-editor shell script."""
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _arc(
    *args: str,
    env: dict[str, str] | None = None,
    stdin_text: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` with a controlled VISUAL/EDITOR environment and EOF-able stdin."""
    full_env = os.environ.copy()
    full_env.pop("VISUAL", None)
    full_env.pop("EDITOR", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
        input=stdin_text,
        env=full_env,
    )


# ---------------------------------------------------------------------------
# arc skill evals <skill_path>  (list — static AST walk, no execution)
# ---------------------------------------------------------------------------


class TestEvalsList:
    def test_list_exits_zero_and_shows_case_nodeids(self, tmp_path: Path) -> None:
        """Listing shows every discovered case nodeid from both eval files."""
        skill_dir = _make_skill(
            tmp_path,
            machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"],
            human_cases=["test_human_1"],
        )
        result = _arc("skill", "evals", str(skill_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert f"{_GENERATED}::test_gen_1" in result.stdout
        assert f"{_HUMAN}::test_human_1" in result.stdout

    def test_list_shows_provenance_per_case(self, tmp_path: Path) -> None:
        """Each case row carries its provenance: machine vs human authored."""
        skill_dir = _make_skill(
            tmp_path,
            machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"],
            human_cases=["test_human_1"],
        )
        result = _arc("skill", "evals", str(skill_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "machine" in result.stdout.lower()
        assert "human" in result.stdout.lower()

    def test_list_never_executes_eval_code(self, tmp_path: Path) -> None:
        """Listing is a static AST walk: a module that explodes at import time still lists."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1"])
        boom = skill_dir / "evals" / "test_boom.py"
        boom.write_text(
            "import definitely_not_a_real_module_xyz\n"
            'raise RuntimeError("must never execute")\n'
            "\n"
            "def test_boom_case():\n"
            "    assert definitely_not_a_real_module_xyz.f() == 1\n"
        )
        result = _arc("skill", "evals", str(skill_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "test_boom.py::test_boom_case" in result.stdout


# ---------------------------------------------------------------------------
# arc skill evals edit <skill_path> <file>
# ---------------------------------------------------------------------------


_APPEND_TEST = """\
cat >> "$1" <<'PYEOF'

def test_appended_by_human():
    assert sorted([2, 1]) == [1, 2]
PYEOF
"""


class TestEvalsEdit:
    def test_valid_save_commits_to_evals_file(self, tmp_path: Path) -> None:
        """A valid editor save is committed into the real evals/ file; exit 0."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        editor = _write_editor(tmp_path / "append.sh", _APPEND_TEST)
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        committed = (skill_dir / "evals" / _GENERATED).read_text()
        assert "test_appended_by_human" in committed

    def test_editor_opens_a_temp_copy_not_the_real_file(self, tmp_path: Path) -> None:
        """The editor is handed a TEMP COPY; the real evals/ path is never opened directly."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        paths_log = tmp_path / "opened_paths.txt"
        editor = _write_editor(tmp_path / "append.sh", f'echo "$1" >> {paths_log}\n{_APPEND_TEST}')
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        opened = paths_log.read_text().strip()
        assert opened, "editor was never invoked"
        real = (skill_dir / "evals" / _GENERATED).resolve()
        assert Path(opened).resolve() != real, "editor must open a temp copy, not the real file"

    def test_human_edit_strips_machine_provenance(self, tmp_path: Path) -> None:
        """REQ-111: after a human edit commits, the file's cases classify human-authored.

        Aligned with evalgate semantics: the manifest entry is removed or its recorded
        hash no longer matches the committed bytes, so load_suite reports
        machine_authored=False for every case in the edited file.
        """
        from arcskill.improver.evalgate import load_suite

        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        before = load_suite(skill_dir)
        assert all(c.machine_authored for c in before), "fixture must start machine-authored"

        editor = _write_editor(tmp_path / "append.sh", _APPEND_TEST)
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        after = [c for c in load_suite(skill_dir) if _GENERATED in c.id]
        assert after, "edited file's cases must still be discovered"
        assert all(not c.machine_authored for c in after), (
            "human edit must strip machine provenance (manifest removed or hash mismatched)"
        )

    def test_visual_takes_precedence_over_editor(self, tmp_path: Path) -> None:
        """$VISUAL wins when both $VISUAL and $EDITOR are set."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        visual_marker = tmp_path / "visual_ran"
        editor_marker = tmp_path / "editor_ran"
        visual = _write_editor(tmp_path / "visual.sh", f"touch {visual_marker}\n{_APPEND_TEST}")
        editor = _write_editor(tmp_path / "editor.sh", f"touch {editor_marker}\n{_APPEND_TEST}")
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"VISUAL": str(visual), "EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert visual_marker.exists(), "$VISUAL editor must be launched"
        assert not editor_marker.exists(), "$EDITOR must not run when $VISUAL is set"

    def test_editor_used_when_visual_unset(self, tmp_path: Path) -> None:
        """$EDITOR is the fallback when $VISUAL is unset."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        editor_marker = tmp_path / "editor_ran"
        editor = _write_editor(tmp_path / "editor.sh", f"touch {editor_marker}\n{_APPEND_TEST}")
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert editor_marker.exists()

    def test_invalid_save_rejected_original_untouched(self, tmp_path: Path) -> None:
        """A save that no longer parses is rejected: nonzero exit, syntax error reported,
        original file bytes untouched on disk (empty stdin declines any reopen prompt)."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        target = skill_dir / "evals" / _GENERATED
        original = target.read_bytes()
        corrupt = _write_editor(tmp_path / "corrupt.sh", "printf 'def broken(:\\n' >> \"$1\"")
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(corrupt)},
        )
        assert result.returncode != 0
        assert "syntax" in result.stderr.lower()
        assert target.read_bytes() == original

    def test_editor_nonzero_exit_aborts_with_no_changes(self, tmp_path: Path) -> None:
        """Git-style abort: editor exits nonzero -> no commit, nonzero exit code."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        target = skill_dir / "evals" / _GENERATED
        original = target.read_bytes()
        abort = _write_editor(tmp_path / "abort.sh", "exit 3")
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(abort)},
        )
        assert result.returncode != 0
        assert "editor" in result.stderr.lower()
        assert target.read_bytes() == original

    def test_commit_leaves_no_temp_residue(self, tmp_path: Path) -> None:
        """Atomic commit (temp + os.replace) leaves no temp files beside eval sources."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        editor = _write_editor(tmp_path / "append.sh", _APPEND_TEST)
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        residue = [
            p.name
            for p in (skill_dir / "evals").iterdir()
            if p.suffix == ".tmp" or ".tmp" in p.name
        ]
        assert residue == []


# ---------------------------------------------------------------------------
# REQ-119 warnings: min_golden_cases floor + passing-anchor reduction, --force
# ---------------------------------------------------------------------------


def _replace_with(names: list[str]) -> str:
    """Editor body that replaces the whole file with a machine-style module of *names*."""
    funcs = "\n\n".join(f"def {n}():\n    assert len('abc') == 3" for n in names)
    module = f"{_MACHINE_DOCSTRING}\n\n{funcs}\n"
    return f"cat > \"$1\" <<'PYEOF'\n{module}PYEOF\n"


class TestEvalsEditWarnings:
    def test_edit_below_min_golden_cases_rejected_without_force(self, tmp_path: Path) -> None:
        """Dropping a code-kind skill's suite below min_golden_cases (3) is rejected."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        target = skill_dir / "evals" / _GENERATED
        original = target.read_bytes()
        editor = _write_editor(tmp_path / "shrink.sh", _replace_with(["test_gen_1", "test_gen_2"]))
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode != 0
        assert "min_golden_cases" in result.stderr
        assert target.read_bytes() == original

    def test_edit_below_min_golden_cases_allowed_with_force(self, tmp_path: Path) -> None:
        """--force allows the below-minimum edit but the warning still prints on stderr."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        editor = _write_editor(tmp_path / "shrink.sh", _replace_with(["test_gen_1", "test_gen_2"]))
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            "--force",
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "min_golden_cases" in result.stderr
        committed = (skill_dir / "evals" / _GENERATED).read_text()
        assert "test_gen_3" not in committed

    def test_edit_reducing_anchor_count_rejected_without_force(self, tmp_path: Path) -> None:
        """Removing an existing case (suite stays >= 3) reduces the passing-anchor count:
        warned and rejected without --force."""
        names = [f"test_gen_{i}" for i in range(1, 6)]
        skill_dir = _make_skill(tmp_path, machine_cases=names)
        target = skill_dir / "evals" / _GENERATED
        original = target.read_bytes()
        editor = _write_editor(tmp_path / "drop_one.sh", _replace_with(names[:-1]))
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert result.returncode != 0
        assert "anchor" in result.stderr.lower()
        assert target.read_bytes() == original

    def test_edit_reducing_anchor_count_allowed_with_force(self, tmp_path: Path) -> None:
        names = [f"test_gen_{i}" for i in range(1, 6)]
        skill_dir = _make_skill(tmp_path, machine_cases=names)
        editor = _write_editor(tmp_path / "drop_one.sh", _replace_with(names[:-1]))
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            "--force",
            env={"EDITOR": str(editor)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anchor" in result.stderr.lower()
        committed = (skill_dir / "evals" / _GENERATED).read_text()
        assert "test_gen_5" not in committed


# ---------------------------------------------------------------------------
# Concurrent-edit guard: improvement pass in flight
# ---------------------------------------------------------------------------


class TestEvalsEditConcurrentGuard:
    def test_edit_warns_when_improvement_pass_in_flight(self, tmp_path: Path) -> None:
        """An on-disk marker (evals/.improver.lock) means an improvement pass is in
        flight: edit warns on stderr before opening the editor, then proceeds."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        (skill_dir / "evals" / ".improver.lock").write_text("{}")
        editor_marker = tmp_path / "editor_ran"
        editor = _write_editor(tmp_path / "append.sh", f"touch {editor_marker}\n{_APPEND_TEST}")
        result = _arc(
            "skill",
            "evals",
            "edit",
            str(skill_dir),
            _GENERATED,
            env={"EDITOR": str(editor)},
        )
        assert "improvement pass" in result.stderr.lower()
        assert editor_marker.exists(), "warned edit still opens the editor"
        assert result.returncode == 0, f"stderr: {result.stderr}"


# ---------------------------------------------------------------------------
# arc skill evals regen <skill_path>
# ---------------------------------------------------------------------------


class TestEvalsRegen:
    def test_regen_shows_diff_preview_and_aborts_without_confirmation(
        self, tmp_path: Path
    ) -> None:
        """regen previews a unified diff of what it would overwrite; with no --yes and
        EOF on stdin the prompt is declined: nonzero exit, nothing changed."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        target = skill_dir / "evals" / _GENERATED
        original = target.read_bytes()
        result = _arc("skill", "evals", "regen", str(skill_dir))
        lines = result.stdout.splitlines()
        assert any(line.startswith("--- ") for line in lines), "missing unified-diff header"
        assert any(line.startswith("+++ ") for line in lines), "missing unified-diff header"
        assert _GENERATED in result.stdout
        assert result.returncode != 0
        assert target.read_bytes() == original

    def test_regen_confirmed_requires_agent_context(self, tmp_path: Path) -> None:
        """The CLI embeds no LLM: a confirmed regen (--yes) errors clearly — regeneration
        needs agent context (LLM invoker + sandbox runner) — and overwrites nothing."""
        skill_dir = _make_skill(tmp_path, machine_cases=["test_gen_1", "test_gen_2", "test_gen_3"])
        target = skill_dir / "evals" / _GENERATED
        original = target.read_bytes()
        result = _arc("skill", "evals", "regen", str(skill_dir), "--yes")
        assert result.returncode != 0
        assert "agent context" in result.stderr.lower()
        assert target.read_bytes() == original


# Mark to avoid unused import warning
_ = pytest
