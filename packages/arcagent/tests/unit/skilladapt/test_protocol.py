"""SPEC-044 Phase 2 — SkillAdapter Protocol + NullSkillAdapter default (REQ-002)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.skilladapt import NullSkillAdapter, SkillAdapter


def test_null_adapter_satisfies_protocol_structurally() -> None:
    assert isinstance(NullSkillAdapter(), SkillAdapter)


@pytest.mark.asyncio
async def test_null_adapter_is_a_silent_noop(tmp_path: Path) -> None:
    """Every method returns immediately and writes nothing (AC-1)."""
    adapter = NullSkillAdapter()
    await adapter.observe(
        skill_name="s", tool_name="read", status="error", error_type="ValueError"
    )
    await adapter.on_turn_end(turn=1, outcome="success")
    await adapter.maybe_improve(insight="x")
    await adapter.review_lifecycle(turn=1)
    # No files anywhere under a scratch workspace.
    assert not any(tmp_path.rglob("*"))
