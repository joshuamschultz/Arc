"""T-024 — weighted graph: Hebbian saturation, salience-slowed decay, spreading."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph

_SCOPE = "did:a"


def test_hebbian_bump_saturates_at_ceiling(db: MemoryDB) -> None:
    graph = WeightedGraph(db, MemoryConfig())
    weights = [graph.hebbian_bump(_SCOPE, "a", "b") for _ in range(200)]

    # Monotonically increasing, never exceeding W, converging to the ceiling.
    assert all(x <= 1.0 + 1e-9 for x in weights)
    assert weights == sorted(weights)
    assert weights[-1] > 0.99
    assert weights[-1] >= weights[-2]  # saturating: later gains vanish


def test_unreinforced_edge_decays_below_floor(db: MemoryDB) -> None:
    cfg = MemoryConfig()
    graph = WeightedGraph(db, cfg)
    ts = datetime.now(UTC).isoformat()
    graph.hebbian_bump(_SCOPE, "a", "b", ts=ts)  # weight ~= 0.3, salience 0

    later = datetime.now(UTC) + timedelta(days=40)
    forgotten = graph.decay(_SCOPE, now=later, lam=cfg.lambda_fast)

    assert forgotten == 1
    assert graph.weight(_SCOPE, "a", "b") == 0.0  # dropped below forget_floor


def test_salient_edge_survives_same_decay(db: MemoryDB) -> None:
    cfg = MemoryConfig()
    graph = WeightedGraph(db, cfg)
    ts = datetime.now(UTC).isoformat()
    graph.hebbian_bump(_SCOPE, "a", "b", salience=1.0, ts=ts)  # salience slows decay

    later = datetime.now(UTC) + timedelta(days=40)
    forgotten = graph.decay(_SCOPE, now=later, lam=cfg.lambda_fast)

    assert forgotten == 0
    assert graph.weight(_SCOPE, "a", "b") > cfg.forget_floor


def test_spreading_activation_reaches_two_hops(db: MemoryDB) -> None:
    graph = WeightedGraph(db, MemoryConfig())
    # chain: src -- mid -- far  (far is 2 hops from src)
    graph.hebbian_bump(_SCOPE, "src", "mid")
    graph.hebbian_bump(_SCOPE, "mid", "far")

    activation = graph.spreading_activation(_SCOPE, {"src": 1.0}, max_hops=2)
    assert activation["mid"] > 0.0
    assert activation.get("far", 0.0) > 0.0  # activation flowed to the 2-hop node

    # hop cap of 1 must NOT reach the 2-hop node.
    shallow = graph.spreading_activation(_SCOPE, {"src": 1.0}, max_hops=1)
    assert shallow.get("far", 0.0) == 0.0
