"""SPEC-044 HIGH-3 — a retired (suppressed) skill is hidden from BOTH offering read sites.

Retirement suppresses the skill in the CapabilityRegistry (the single source of truth), so
it is excluded from the arcrun advertisement (``_agent_skills``) AND the prompt manifest
(``format_for_prompt``), survives a disk re-scan (``register_skill`` skips it), and is
restored on revive (REQ-043).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.capabilities.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.core.agent_dispatch import _agent_skills


def _entry(name: str) -> SkillEntry:
    return SkillEntry(
        name=name,
        version="1.0.0",
        description=f"{name} desc",
        triggers=(),
        tools=(),
        location=Path(f"skills/{name}/SKILL.md"),
        scan_root="builtin",
    )


async def _registry(*names: str) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    for name in names:
        await reg.register_skill(_entry(name))
    return reg


class _Agent:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._capability_registry = registry


@pytest.mark.asyncio
async def test_suppress_hides_from_advertisement_and_manifest() -> None:
    reg = await _registry("fresh", "stale", "other")
    await reg.suppress_skill("stale")

    # Read site 1 — the arcrun capability advertisement.
    offered = {s.name for s in _agent_skills(_Agent(reg))}  # type: ignore[arg-type]
    assert offered == {"fresh", "other"}
    # Read site 2 — the rendered prompt manifest.
    manifest = await reg.format_for_prompt()
    assert "stale" not in manifest
    assert "fresh" in manifest


@pytest.mark.asyncio
async def test_suppression_survives_rescan() -> None:
    reg = await _registry("stale")
    await reg.suppress_skill("stale")
    # A disk re-scan re-registers the same skill; suppression must hold (no re-offer).
    await reg.register_skill(_entry("stale"))
    assert {s.name for s in _agent_skills(_Agent(reg))} == set()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_revive_restores_offering() -> None:
    reg = await _registry("stale")
    await reg.suppress_skill("stale")
    await reg.unsuppress_skill("stale")
    assert {s.name for s in _agent_skills(_Agent(reg))} == {"stale"}  # type: ignore[arg-type]
    assert await reg.suppressed_skills() == frozenset()
