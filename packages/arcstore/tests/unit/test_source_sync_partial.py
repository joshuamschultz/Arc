"""SPEC-082 COMP-010 (T-1095 RED) — the partial-run fact is durable.

REQ-429: when a sync run stops at a page/byte/time budget, the partial-run
(``budget_reached``) fact SHALL be persisted durably, so a crash does not lose
that the run was partial and reconcile/tombstoning stays correctly suppressed on
resume.

Today ``budget_reached`` lives only in the coordinator's local memory
(``coordinator.run``); ``SourceSyncState`` has no such field, and its
``model_config`` is ``extra="forbid"``. A process that stops at a budget and
then restarts reads a state that looks like a completed full crawl — so the next
run reconciles a partial listing and tombstones every object it never reached.

These tests fail RED because the durable field does not exist yet.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcstore.source_sync import (
    ArcStoreSourceSyncStore,
    SourceSyncState,
    SourceSyncStatus,
)


def test_state_carries_a_durable_partial_flag_defaulting_false() -> None:
    """A fresh state is a full crawl until something says otherwise."""
    state = SourceSyncState(agent_did="did:a", source_id="source")
    assert state.budget_reached is False


def test_partial_flag_round_trips_through_serialization() -> None:
    """Durable means it survives the dump/validate that a restart replays.

    The Postgres backend rehydrates a run's state with
    ``SourceSyncState.model_validate(row)``. A field that does not round-trip is a
    field a restart forgets — exactly the loss this requirement forbids.
    """
    partial = SourceSyncState(
        agent_did="did:a",
        source_id="source",
        status=SourceSyncStatus.RUNNING,
        budget_reached=True,
    )

    restored = SourceSyncState.model_validate(partial.model_dump(mode="json"))

    assert restored.budget_reached is True


@pytest.mark.asyncio
async def test_store_restores_partial_flag_after_a_simulated_restart() -> None:
    """A run that stopped at a budget is still partial after the process restarts.

    The durable backend row carries ``budget_reached``; the store must surface it
    on the next read so a resuming coordinator suppresses full-crawl tombstoning
    instead of deleting everything the partial run never reached.
    """

    class _RestartedBackend:
        """A durable row persisted before the (simulated) crash."""

        def __init__(self, row: dict[str, Any]) -> None:
            self._row = row

        async def source_sync_get_state(self, agent_did: str, source_id: str) -> dict[str, Any]:
            return self._row

    persisted = {
        "agent_did": "did:a",
        "source_id": "source",
        "cursor": "page-3",
        "status": SourceSyncStatus.RUNNING.value,
        "budget_reached": True,
    }
    store = ArcStoreSourceSyncStore(_RestartedBackend(persisted))  # type: ignore[arg-type]

    state = await store.get_state("did:a", "source")

    assert state.budget_reached is True, "the partial-run fact did not survive the restart"
