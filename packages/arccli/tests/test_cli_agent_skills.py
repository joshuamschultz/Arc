"""Task #39 (fold-in) — `arc agent skills` must mirror reality, with a status column.

Live bug: `arc agent skills` showed 1 skill with no status column while arcui's
capability view (backed by `collect_agent_capability_inventory`) correctly
showed 34 with verdicts. Root cause matches task #29's `arc agent tools` bug
exactly: `_skills()` used `_iter_skill_folders` (agent's own + global +
workspace scan roots ONLY — explicitly excludes package builtins, per its own
docstring) plus a bare `validate_skill_folder()` call with no verdict surfaced
at all.

Fixed by switching to the same read-only inventory seam arcui already uses —
`collect_agent_capability_inventory` — filtered to `kind == "skill"`, with
`status`/`status_detail` rendered as a real column.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from arccli.commands.agent._dispatch import agent_handler

_VALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 1.0.0\n"
    "description: does {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)


def _write_agent(tmp_path: Path) -> Path:
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n', encoding="utf-8"
    )
    return tmp_path


def _write_skill(root: Path, name: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(_VALID_SKILL.format(name=name), encoding="utf-8")


def test_skills_lists_builtins_skills_plus_agent_authored(tmp_path: Path) -> None:
    """Builtin skills (create-skill, create-tool, update-skill, update-tool —
    shipped under arcagent/builtins/capabilities/skills/) must appear
    alongside an agent-authored one — the exact gap that made the count
    show 1 instead of the real total.
    """
    agent = _write_agent(tmp_path)
    _write_skill(agent / "capabilities" / "skills", "my-skill")

    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["skills", str(agent)])
    text = out.getvalue()

    assert "my-skill" in text
    assert "create-skill" in text or "create_skill" in text


def test_skills_shows_a_status_column(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path)
    _write_skill(agent / "capabilities" / "skills", "my-skill")

    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["skills", str(agent)])
    text = out.getvalue()

    assert "Status" in text
    assert "loaded" in text


def test_skills_no_authored_skills_still_shows_builtins(tmp_path: Path) -> None:
    """Even with zero agent-authored skills, the agent still boots with
    builtin skills — matching arc agent tools' equivalent regression guard.
    """
    agent = _write_agent(tmp_path)

    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["skills", str(agent)])
    text = out.getvalue()

    assert "create-skill" in text or "create_skill" in text
