"""SPEC-044 Phase 2 — skills-module wiring through the REAL hook path (AC-1).

Drives the actual ``agent:*`` hook handlers (not a direct adapter call) so the test
proves the extension is *wired*, not merely present (producers-unwired defense).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.capabilities.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import (
    skills_post_plan,
    skills_post_tool,
    skills_pre_respond,
    skills_ready,
)


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


async def _registry(*skills: tuple[str, Path]) -> CapabilityRegistry:
    """Build a real CapabilityRegistry (finding 3b: the module reads its real shape)."""
    reg = CapabilityRegistry()
    for name, loc in skills:
        await reg.register_skill(
            SkillEntry(
                name=name, version="1.0.0", description=name, triggers=(), tools=(),
                location=loc, scan_root="builtin",
            )
        )
    return reg


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.mark.asyncio
async def test_ac1_default_is_silent_noop(tmp_path: Path) -> None:
    """Default (adapter='none') → hooks short-circuit, zero improver files (AC-1)."""
    _runtime.configure(config={}, workspace=tmp_path)
    assert _runtime.state().active is False

    await skills_ready(_Ctx(skill_registry=await _registry()))
    await skills_post_tool(_Ctx(tool="read", args={"file_path": str(tmp_path / "SKILL.md")}))
    await skills_post_tool(_Ctx(tool="bash", result="ok"))
    await skills_post_plan(_Ctx(task_outcome="success", turn_number=1))
    await skills_pre_respond(_Ctx())

    assert not (tmp_path / "skill_traces").exists()
    assert not any(tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_live_path_collects_traces_via_hooks(tmp_path: Path) -> None:
    """arcskill adapter: a read + tool calls + turn end persist a real trace JSONL.

    Proves the observe → on_turn_end wiring is LIVE end-to-end via the hooks.
    """
    skill_file = tmp_path / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# my-skill\nsteps\n", encoding="utf-8")

    _runtime.configure(config={"adapter": "arcskill"}, workspace=tmp_path)
    assert _runtime.state().active is True

    await skills_ready(_Ctx(skill_registry=await _registry(("my-skill", skill_file))))
    # Read the skill (opens the span), then two tool calls, then close the turn.
    await skills_post_tool(_Ctx(tool="read", args={"file_path": str(skill_file)}))
    await skills_post_tool(_Ctx(tool="bash", result="ok"))
    await skills_post_tool(_Ctx(tool="grep", result="ok"))
    await skills_post_plan(_Ctx(task_outcome="success", turn_number=1))

    traces_dir = tmp_path / "skill_traces" / "my-skill"
    jsonl = list(traces_dir.glob("traces-*.jsonl"))
    assert jsonl, "expected a persisted trace for the used skill"
    body = jsonl[0].read_text(encoding="utf-8")
    assert '"skill_name": "my-skill"' in body
    assert '"tool_name": "bash"' in body
