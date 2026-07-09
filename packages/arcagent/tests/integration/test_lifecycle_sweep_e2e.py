"""SPEC-044 AC-5 (CRITICAL-1) + finding 3b — the Curator sweep runs on the real producer.

Producers-unwired defense for the lifecycle sweep: an inactive skill is retired only when
the ``@background_task`` sweep loop's poll body (``_runtime.run_lifecycle_sweep``) runs →
adapter ``review_lifecycle`` → retire → reconcile suppresses it in the REAL
CapabilityRegistry (so it stops being offered) → operator-signed WORM audit. No direct
facade ``review_lifecycle()`` call.

Finding 3b: the skills module reads the REAL CapabilityRegistry (``_skills`` dict,
``SkillEntry.location``) delivered at ``agent:ready`` — so ``_skill_path`` resolves in
production, not just against a test double.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arctrust import OperatorKey, verify_chain

from arcagent.capabilities.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.core.agent_dispatch import _agent_skills
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import skills_ready


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


class _Agent:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._capability_registry = registry


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _seed_inactive_trace(ws: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=60)  # older than the 30-day window
    traces_dir = ws / "skill_traces" / "old-skill"
    traces_dir.mkdir(parents=True)
    trace = {
        "trace_id": "t",
        "session_id": "s",
        "skill_name": "old-skill",
        "skill_version": 0,
        "turn_number": 0,
        "started_at": old.isoformat(),
        "ended_at": old.isoformat(),
        "tool_calls": [],
        "task_outcome": "success",
    }
    (traces_dir / "traces-2020-01.jsonl").write_text(json.dumps(trace) + "\n", encoding="utf-8")


async def _registry_with(name: str, location: Path) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    await reg.register_skill(
        SkillEntry(
            name=name,
            version="1.0.0",
            description=name,
            triggers=(),
            tools=(),
            location=location,
            scan_root="builtin",
        )
    )
    return reg


@pytest.mark.asyncio
async def test_ac5_background_sweep_retires_and_suppresses_with_operator_audit(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "agent"
    ws.mkdir()
    _seed_inactive_trace(ws)
    skill_md = ws / "skills" / "old-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# old-skill\n", encoding="utf-8")
    operator = OperatorKey.generate()

    _runtime.configure(
        config={"adapter": "arcskill", "tier": "personal"},
        workspace=ws,
        operator_signer=operator.into_signer(),
    )
    assert _runtime.state().active is True

    registry = await _registry_with("old-skill", skill_md)
    await skills_ready(_Ctx(skill_registry=registry))

    # 3b: the real registry is wired — _skill_path resolves to the real SKILL.md location.
    assert _runtime._skill_path("old-skill") == skill_md

    # Drive the background loop's poll body — the real producer (not a facade call).
    await _runtime.run_lifecycle_sweep()

    # Retired + suppressed: gone from the offering, and audited on the operator WORM chain.
    assert _runtime.state().adapter.retired_skills() == frozenset({"old-skill"})
    assert await registry.suppressed_skills() == frozenset({"old-skill"})
    assert {s.name for s in _agent_skills(_Agent(registry))} == set()  # type: ignore[arg-type]

    chain = ws.parent / ".audit" / "skills.worm"
    assert chain.exists(), "retire must emit an operator-signed WORM audit event"
    assert verify_chain(chain, operator.public_key) is True
