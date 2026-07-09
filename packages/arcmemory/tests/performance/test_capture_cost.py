"""T-032 — capture cost is flat across a 10k-item store (constant-time)."""

from __future__ import annotations

import time
from pathlib import Path

from arcmemory.capture import FastCapture
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.types import Scope


def _cap(workspace: Path, db: MemoryDB, scope: Scope) -> FastCapture:
    return FastCapture(
        db,
        workspace,
        scope,
        WeightedGraph(db),
        seed_vocabulary=["alice", "bob"],
    )


def _time_capture(cap: FastCapture, text: str) -> float:
    start = time.perf_counter()
    cap.capture(text)
    return time.perf_counter() - start


def test_capture_latency_is_flat_over_10k_items(workspace, db, scope) -> None:
    cap = _cap(workspace, db, scope)

    baseline = min(_time_capture(cap, f"alice bob early item {i}") for i in range(20))

    for i in range(10_000):
        cap.capture(f"alice bob bulk fill item {i}")

    tail = min(_time_capture(cap, f"alice bob late item {i}") for i in range(20))

    # Constant-cost: a capture into a 10k-row store is not meaningfully slower
    # than into an empty one. Generous multiplier absorbs scheduler jitter.
    assert tail < baseline * 8 + 0.01, f"capture cost grew with store size: {baseline=} {tail=}"
