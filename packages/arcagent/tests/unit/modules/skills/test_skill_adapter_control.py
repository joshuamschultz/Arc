"""alpha-2 P8 — operator surfaces reach the running agent's skill adapter.

The arcui routes run in a request task, not the agent task that configured the
skills module. Two things must hold for them to work:

* the adapter resolves skill paths from ITS agent's state, not from whichever
  context happens to call it (a fresh context must still find the skill);
* ``ArcAgent.skill_adapter()`` hands out the adapter registered under the
  ``skill_adapter`` operation contract, and a :class:`NullSkillAdapter` (typed
  ``unavailable`` answers) when the skills module is off or the agent is not started.
"""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from typing import Any

import pytest

from arcagent.core.agent import ArcAgent
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import SkillAdapterControl, skills_ready
from arcagent.skilladapt import NullSkillAdapter


class _Entry:
    def __init__(self, name: str, location: Path) -> None:
        self.name = name
        self.location = location


class _Registry:
    def __init__(self, entry: _Entry) -> None:
        self._entry = entry

    def skill_entries(self) -> list[_Entry]:
        return [self._entry]

    def skill_entry(self, name: str) -> _Entry | None:
        return self._entry if name == self._entry.name else None

    async def suppressed_skills(self) -> list[str]:
        return []

    async def suppress_skill(self, name: str) -> None:
        return None

    async def unsuppress_skill(self, name: str) -> None:
        return None


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


async def _configured(tmp_path: Path) -> Path:
    skill_md = tmp_path / "skills" / "s" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: s\n---\n# s\n", encoding="utf-8")
    _runtime.configure(config={"adapter": "arcskill"}, workspace=tmp_path / "ws")
    await skills_ready(_Ctx(skill_registry=_Registry(_Entry("s", skill_md))))
    return skill_md


@pytest.mark.asyncio
async def test_adapter_finds_skills_from_a_context_that_never_configured_it(
    tmp_path: Path,
) -> None:
    await _configured(tmp_path)
    adapter = _runtime.state().adapter

    task = asyncio.get_running_loop().create_task(
        adapter.run_evals(skill_name="s"), context=contextvars.Context()
    )
    result = await task

    assert result["status"] == "no_suite", result


@pytest.mark.asyncio
async def test_control_capability_exposes_the_configured_adapter(tmp_path: Path) -> None:
    await _configured(tmp_path)
    control = SkillAdapterControl()
    await control.setup(None)

    assert control.adapter is _runtime.state().adapter


class _CapEntry:
    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.setup_done = True


class _CapRegistry:
    def __init__(self, instance: Any) -> None:
        self._instance = instance
        self.asked: list[str] = []

    async def get_capability(self, name: str) -> _CapEntry | None:
        self.asked.append(name)
        return _CapEntry(self._instance) if self._instance is not None else None


def _agent(*, started: bool, control: Any) -> ArcAgent:
    agent = ArcAgent.__new__(ArcAgent)
    agent._started = started
    agent._capability_registry = _CapRegistry(control)  # type: ignore[assignment]  # reason: fake registry
    return agent


@pytest.mark.asyncio
async def test_agent_hands_out_the_registered_adapter(tmp_path: Path) -> None:
    await _configured(tmp_path)
    control = SkillAdapterControl()
    await control.setup(None)
    agent = _agent(started=True, control=control)

    adapter = await agent.skill_adapter()

    assert adapter is _runtime.state().adapter
    assert agent._capability_registry.asked == ["skill_adapter"]  # type: ignore[union-attr]  # reason: fake registry


@pytest.mark.asyncio
async def test_agent_without_the_skills_module_hands_out_the_null_adapter() -> None:
    adapter = await _agent(started=True, control=None).skill_adapter()
    assert isinstance(adapter, NullSkillAdapter)
    result = await adapter.improve_now(skill_name="s", dry_run=True)
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_agent_not_started_hands_out_the_null_adapter() -> None:
    adapter = await _agent(started=False, control=object()).skill_adapter()
    assert isinstance(adapter, NullSkillAdapter)
