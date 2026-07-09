"""SPEC-044 CRITICAL-2 (arcagent side) — SkillApprover + configure wiring.

The improver's tier approval ladder is decided in arcskill; here we verify the arcagent
side supplies a real, fail-closed approver and threads the agent DID so the audit actor is
correct. No interactive channel is wired yet (SPEC-032), so federal/enterprise mutations
fail closed by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.skills import _runtime
from arcagent.modules.skills.approver import SkillApprover


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.mark.asyncio
async def test_approver_fails_closed_without_channel() -> None:
    approver = SkillApprover()
    assert await approver.request(action="skill.mutation", skill_name="s", detail="x") is False


@pytest.mark.asyncio
async def test_approver_consults_channel() -> None:
    async def _grant(action: str, skill_name: str, detail: str) -> bool:
        return True

    approver = SkillApprover(channel=_grant)
    assert await approver.request(action="skill.mutation", skill_name="s", detail="x") is True


@pytest.mark.asyncio
async def test_approver_channel_error_fails_closed() -> None:
    async def _boom(action: str, skill_name: str, detail: str) -> bool:
        raise RuntimeError("channel down")

    approver = SkillApprover(channel=_boom)
    assert await approver.request(action="skill.mutation", skill_name="s", detail="x") is False


def test_configure_wires_approver_and_agent_did(tmp_path: Path) -> None:
    """The arcskill adapter is constructed with a real approver + the agent DID (federal)."""
    _runtime.configure(
        config={"adapter": "arcskill", "tier": "federal"},
        workspace=tmp_path,
        agent_did="did:arc:test-agent",
    )
    adapter = _runtime.state().adapter
    assert isinstance(adapter._approver, SkillApprover)  # real approver wired
    assert adapter._agent_did == "did:arc:test-agent"  # audit actor threaded
    assert adapter.tier == "federal"
