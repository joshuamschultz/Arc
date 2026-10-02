"""Durable per-node state on the Run record, written by revision CAS (P14-B step 1).

``Run.node_states`` is the UI- and resume-facing snapshot of every node; the
write is a whole-dict patch guarded by ``where={"revision": expected}`` so two
writers holding the same snapshot can never both land.

Per [[feedback_concurrency_tests_must_interleave]] the race test parks both
writers on an ``asyncio.Barrier`` immediately before the conditional write, so
both are provably holding the same revision when the store decides.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from arcstore.backends.memory import FakeBackend
from arcstore.runs import NodeState, Run, RunStore

_INITIATOR = "did:arc:test:human/operator"
_RUNNER = "did:arc:test:exec/runner00"


async def _store_with_run(backend: FakeBackend, run_id: str = "run-ns") -> RunStore:
    store = RunStore(backend)
    await store.create(
        Run(
            id=run_id,
            workflow_id="wf",
            workflow_version=1,
            content_hash="sha256:x",
            status="running",
            initiator_did=_INITIATOR,
        )
    )
    return store


class _BarrierBackend(FakeBackend):
    """Holds every ``runs`` CAS until both writers have arrived."""

    def __init__(self, parties: int) -> None:
        super().__init__()
        self.barrier = asyncio.Barrier(parties)
        self.armed = False

    async def update_if(self, collection: str, key: str, *args: Any, **kwargs: Any) -> bool:
        if self.armed and collection == "runs":
            await self.barrier.wait()
        return await super().update_if(collection, key, *args, **kwargs)


async def test_new_run_starts_at_revision_zero_with_no_node_states() -> None:
    backend = FakeBackend()
    store = await _store_with_run(backend)
    run = await store.get("run-ns")
    assert run is not None
    assert run.revision == 0
    assert run.node_states == {}


async def test_set_node_states_merges_and_bumps_revision() -> None:
    backend = FakeBackend()
    store = await _store_with_run(backend)
    first, outcome = await store.set_node_states(
        "run-ns",
        {"a": NodeState(status="materialized", task_id="t-a")},
        actor_did=_RUNNER,
        expected_revision=0,
    )
    assert outcome == "applied"
    assert first is not None and first.revision == 1
    second, outcome = await store.set_node_states(
        "run-ns",
        {"b": NodeState(status="skipped", reason="condition")},
        actor_did=_RUNNER,
        expected_revision=1,
    )
    assert outcome == "applied"
    assert second is not None and second.revision == 2
    assert set(second.node_states) == {"a", "b"}
    assert second.node_states["a"].task_id == "t-a"


async def test_set_node_states_is_cas_on_revision() -> None:
    backend = _BarrierBackend(parties=2)
    await backend.start()
    store = await _store_with_run(backend)
    backend.armed = True
    results = await asyncio.gather(
        store.set_node_states(
            "run-ns",
            {"a": NodeState(status="done")},
            actor_did=_RUNNER,
            expected_revision=0,
        ),
        store.set_node_states(
            "run-ns",
            {"a": NodeState(status="failed", last_error="stale writer")},
            actor_did=_RUNNER,
            expected_revision=0,
        ),
    )
    backend.armed = False
    outcomes = sorted(outcome for _, outcome in results)
    assert outcomes == ["applied", "conflict"]
    stored = await store.get("run-ns")
    assert stored is not None and stored.revision == 1
    winner = next(run for run, outcome in results if outcome == "applied")
    assert winner is not None
    assert stored.node_states == winner.node_states


async def test_stale_revision_cannot_overwrite_node_states() -> None:
    backend = FakeBackend()
    store = await _store_with_run(backend)
    await store.set_node_states(
        "run-ns", {"a": NodeState(status="done")}, actor_did=_RUNNER, expected_revision=0
    )
    forged, outcome = await store.set_node_states(
        "run-ns",
        {"a": NodeState(status="failed", last_error="forged")},
        actor_did=_RUNNER,
        expected_revision=0,
    )
    assert (forged, outcome) == (None, "conflict")
    stored = await store.get("run-ns")
    assert stored is not None and stored.node_states["a"].status == "done"


async def test_set_node_states_on_missing_run_is_not_found() -> None:
    store = RunStore(FakeBackend())
    assert await store.set_node_states(
        "nope", {"a": NodeState(status="done")}, actor_did=_RUNNER, expected_revision=0
    ) == (None, "not_found")


async def test_run_written_before_revision_existed_gets_its_first_write() -> None:
    """A run row created before this field existed must not be stuck in conflict forever."""
    backend = FakeBackend()
    store = await _store_with_run(backend)
    raw = await backend.mutable_read("runs", "run-ns")
    assert raw is not None
    legacy = {k: v for k, v in raw.items() if k not in ("revision", "node_states")}
    await backend.mutable_write("runs", "run-ns", legacy, actor_did=_RUNNER)
    updated, outcome = await store.set_node_states(
        "run-ns", {"a": NodeState(status="done")}, actor_did=_RUNNER, expected_revision=0
    )
    assert outcome == "applied"
    assert updated is not None and updated.revision == 1


def test_node_state_is_closed() -> None:
    with pytest.raises(ValidationError):
        NodeState(status="exploded")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        NodeState(status="done", surprise=True)  # type: ignore[call-arg]
