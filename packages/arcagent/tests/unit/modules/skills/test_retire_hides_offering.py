"""SPEC-044 HIGH-3 — a retired skill is hidden from the agent's capability offering.

The lifecycle_state produced by the improver is *consumed*: the offering path
(``agent_dispatch._agent_skills``) filters retired skills via the skills-module adapter,
so a retired skill is neither advertised nor loadable until revived (REQ-043).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.agent_dispatch import _agent_skills
from arcagent.modules.skills import _runtime


class _Adapter:
    """Minimal SkillAdapter stand-in exposing retirement state."""

    def __init__(self, retired: set[str]) -> None:
        self._retired = retired

    def retired_skills(self) -> frozenset[str]:
        return frozenset(self._retired)


class _SkillEntry:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} desc"
        self.location = Path(f"skills/{name}/SKILL.md")
        self.scan_root = "builtin"


class _Registry:
    def __init__(self, names: list[str]) -> None:
        self._skills = {n: _SkillEntry(n) for n in names}


class _Agent:
    def __init__(self, names: list[str]) -> None:
        self._capability_registry = _Registry(names)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _install_adapter(retired: set[str]) -> None:
    _runtime._state = _runtime._State(
        adapter=_Adapter(retired), active=True, workspace=Path(".")
    )


def test_retired_skill_excluded_from_offering() -> None:
    _install_adapter({"stale"})
    agent = _Agent(["fresh", "stale", "other"])
    offered = {s.name for s in _agent_skills(agent)}  # type: ignore[arg-type]
    assert offered == {"fresh", "other"}  # 'stale' hidden


def test_revived_skill_reappears_in_offering() -> None:
    _install_adapter(set())  # nothing retired (post-revive)
    agent = _Agent(["fresh", "stale"])
    offered = {s.name for s in _agent_skills(agent)}  # type: ignore[arg-type]
    assert offered == {"fresh", "stale"}


def test_offering_unaffected_when_skills_module_inactive() -> None:
    # No skills runtime configured → retired_skill_names() returns empty, all offered.
    agent = _Agent(["a", "b"])
    offered = {s.name for s in _agent_skills(agent)}  # type: ignore[arg-type]
    assert offered == {"a", "b"}
