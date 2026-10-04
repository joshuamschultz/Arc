"""The memory module's background doc-embed backfill loop.

Connected-document chunks written while the embedder could not serve are stored
lexical-only. The Brain owns the retry (``maintain_doc_embeddings``: one bounded
tick, returning how long to wait); this loop only drives it off the turn path,
obeys the operator kill switch, and outlives any one failed tick.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.modules.memory import _runtime
from arcagent.modules.memory import capabilities as cap

pytestmark = pytest.mark.anyio

_DID = "did:arc:test:backfill-loop"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _BackfillBrain(NullBrain):
    """A brain whose backfill reports a fixed next delay."""

    def __init__(self, delay: float = 0.5, error: Exception | None = None) -> None:
        self.ticks = 0
        self._delay = delay
        self._error = error

    async def maintain_doc_embeddings(self) -> float:
        self.ticks += 1
        if self._error is not None:
            raise self._error
        return self._delay


def _bind(brain: Any) -> None:
    from arcagent.modules.memory.config import MemoryConfig

    _runtime.bind(
        _runtime._State(
            config=MemoryConfig(),
            brain=brain,
            workspace=Path("."),
            telemetry=None,
            bus=None,
            agent_did=_DID,
            active=type(brain) is not NullBrain,
        )
    )


class _LoopStop(Exception):  # noqa: N818 — test sentinel, not a real error
    """Break the `while True` after exactly one iteration."""


async def _one_iteration(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise _LoopStop

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_LoopStop):
        await cap.memory_doc_embed_backfill_loop(None)
    return slept


async def test_one_tick_returns_the_brains_paced_delay() -> None:
    brain = _BackfillBrain(delay=0.5)
    _bind(brain)
    assert await cap.doc_embed_backfill_once() == 0.5
    assert brain.ticks == 1


async def test_a_brain_without_a_backfill_idles() -> None:
    _bind(NullBrain())
    assert await cap.doc_embed_backfill_once() == cap._DOC_BACKFILL_IDLE_SECONDS


async def test_the_loop_sleeps_what_the_tick_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cap._CONSOLIDATE_OFF_ENV, raising=False)
    brain = _BackfillBrain(delay=0.25)
    _bind(brain)
    assert await _one_iteration(monkeypatch) == [0.25]
    assert brain.ticks == 1


async def test_the_kill_switch_stops_the_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cap._CONSOLIDATE_OFF_ENV, "1")
    brain = _BackfillBrain()
    _bind(brain)
    assert await _one_iteration(monkeypatch) == [cap._DOC_BACKFILL_IDLE_SECONDS]
    assert brain.ticks == 0


async def test_a_failed_tick_never_kills_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cap._CONSOLIDATE_OFF_ENV, raising=False)
    _bind(_BackfillBrain(error=RuntimeError("disk full")))
    assert await _one_iteration(monkeypatch) == [cap._DOC_BACKFILL_IDLE_SECONDS]


def test_the_loop_is_a_registered_background_task() -> None:
    meta = cap.memory_doc_embed_backfill_loop._arc_capability_meta  # type: ignore[attr-defined]  # reason: decorator-stamped metadata
    assert meta.name == "memory_doc_embed_backfill"
