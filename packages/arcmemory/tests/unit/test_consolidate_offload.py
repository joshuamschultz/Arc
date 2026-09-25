"""The nightly "sleep" must never pin the single event-loop thread.

The consolidator's three de-dup passes each run an O(N^2) pure-Python cosine sweep
(`_candidate_clusters`, `_cluster_cues`, `_procedure_clusters`). On an idle node this
fires in the quiet window over the whole store; run directly on the loop it saturates
the one asyncio thread, and NATS/JetStream replies and the arcui websocket auth
handshake starve — the "Auth timeout or invalid message" incident (2026-09-14).

These tests prove the CPU sweep is offloaded off the loop: while the sweep is busy,
an unrelated coroutine still makes progress. If the sweep runs on the loop the ticker
freezes and the assertion fails.
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.distill import (
    DaySummaryDraft,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Fact, Insight, Procedure

# The blocking sweep sleeps this long; the ticker fires every _TICK. Offloaded, the
# ticker gets ~_BLOCK/_TICK turns; blocked on the loop it gets at most one.
_BLOCK = 0.3
_TICK = 0.01
_MIN_TICKS = 5


class _NullDistiller:
    """Distiller the constructor needs but the de-dup passes never call."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list[Fact]) -> InsightMint:
        return InsightMint()

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()


class _AllOnes:
    """Embedder that puts every text on the same vector (everything is a candidate)."""

    _DIM: ClassVar[int] = 4

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * self._DIM for _ in texts]


class _ConfirmNothing:
    """Confirmer that declines every cluster (we time the sweep, not the merge)."""

    async def confirm_entity_merges(self, groups: list) -> list[list[str]]:
        return []

    async def find_contradictions(self, refs: list) -> list[str]:
        return [r.slug for r in refs]  # contradict all -> nothing folds


async def _ticks_during(coro) -> int:
    """Run ``coro`` while counting how many times a 10ms ticker gets to run.

    A high count means the loop stayed responsive while ``coro`` did its heavy work;
    a count near zero means the work blocked the loop.
    """
    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            await asyncio.sleep(_TICK)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        await coro
    finally:
        stop = True
        await t
    return ticks


def _block_returning(value: object):
    """A synchronous stand-in for a heavy sweep: it burns wall-clock, then returns."""

    def _blocking(*_a: object, **_k: object) -> object:
        time.sleep(_BLOCK)
        return value

    return _blocking


async def test_entity_clustering_does_not_block_the_loop(
    workspace, db, scope, monkeypatch
) -> None:
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("a", "p", "1", name="Same Name", entity_type="place")
    store.write_fact("b", "p", "2", name="Same Name", entity_type="place")

    consolidator = Consolidator(
        db,
        workspace,
        scope,
        distiller=_NullDistiller(),
        config=MemoryConfig(),
        embedder=_AllOnes(),
        confirmer=_ConfirmNothing(),
    )
    monkeypatch.setattr(consolidator, "_candidate_clusters", _block_returning([]))

    ticks = await _ticks_during(consolidator.merge_entities())
    assert ticks >= _MIN_TICKS, f"entity clustering blocked the loop ({ticks} ticks)"


async def test_cue_clustering_does_not_block_the_loop(workspace, db, scope, monkeypatch) -> None:
    store = InsightStore(workspace)
    for iid, cue in [("i-a", "cue-one"), ("i-b", "cue-two")]:
        store.write(Insight(id=iid, statement="s", trigger="t", cues=[cue], instances=[iid]))

    consolidator = Consolidator(
        db,
        workspace,
        scope,
        distiller=_NullDistiller(),
        config=MemoryConfig(),
        embedder=_AllOnes(),
    )
    monkeypatch.setattr(consolidator, "_cluster_cues", _block_returning({}))

    ticks = await _ticks_during(consolidator.merge_cues())
    assert ticks >= _MIN_TICKS, f"cue clustering blocked the loop ({ticks} ticks)"


async def test_procedure_clustering_does_not_block_the_loop(
    workspace, db, scope, monkeypatch
) -> None:
    store = ProceduralStore(workspace)
    store.upsert("p-a", "Method A", steps=["x"], when_to_use="same trigger")
    store.upsert("p-b", "Method B", steps=["y"], when_to_use="same trigger")

    consolidator = Consolidator(
        db,
        workspace,
        scope,
        distiller=_NullDistiller(),
        config=MemoryConfig(),
        embedder=_AllOnes(),
        confirmer=_ConfirmNothing(),
    )
    monkeypatch.setattr(consolidator, "_procedure_clusters", _block_returning([]))

    ticks = await _ticks_during(consolidator.merge_duplicate_procedures())
    assert ticks >= _MIN_TICKS, f"procedure clustering blocked the loop ({ticks} ticks)"
