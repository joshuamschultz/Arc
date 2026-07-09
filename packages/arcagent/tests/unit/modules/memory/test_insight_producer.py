"""SPEC-044 MED-4 — the memory module produces the skills-improver ``insight``.

The ``agent:pre_respond`` hook (priority 100, runs before the skills reader at 150) sets
``ctx.data["insight"]`` from the active Brain's retrieval; with memory off (NullBrain) it
sets nothing, so the improver stays fully memory-less (REQ-060).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import inject_insight
from arcagent.modules.memory.config import MemoryConfig

_DID = "did:arc:test"


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


class _Brain:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def retrieve(self, query: str, **_: Any) -> str:
        self.queries.append(query)
        return f"recurring failure near: {query}"


def _install(brain: Any) -> None:
    _runtime._state = _runtime._State(
        config=MemoryConfig(),
        brain=brain,
        workspace=Path("."),
        telemetry=None,
        bus=None,
        agent_did=_DID,
        active=not isinstance(brain, NullBrain),
    )


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.mark.asyncio
async def test_insight_populated_from_active_brain() -> None:
    brain = _Brain()
    _install(brain)
    ctx = _Ctx(task="calc skill keeps raising AssertionError")
    await inject_insight(ctx)
    assert brain.queries == ["calc skill keeps raising AssertionError"]
    assert ctx.data["insight"] == "recurring failure near: calc skill keeps raising AssertionError"


@pytest.mark.asyncio
async def test_insight_absent_when_memory_less() -> None:
    _install(NullBrain())
    ctx = _Ctx(task="anything")
    await inject_insight(ctx)
    assert "insight" not in ctx.data  # improver runs memory-less


@pytest.mark.asyncio
async def test_insight_skipped_without_task() -> None:
    _install(_Brain())
    ctx = _Ctx()  # no task in scope
    await inject_insight(ctx)
    assert "insight" not in ctx.data
