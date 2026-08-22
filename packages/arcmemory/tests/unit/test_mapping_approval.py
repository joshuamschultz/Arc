"""COMP-003 (T-1038 RED / T-1039 GREEN) — mapping proposal + SPEC-035 approval.

Drives the REAL SPEC-035 approval path: an :class:`~arcstore.approvals.ApprovalStore`
over a real sqlite backend (``open_backend('sqlite', ...)``), never a mock. A
proposed source-to-home mapping must be staged as a pending row and stay
UN-committable until an operator resolves it ``approved``; once approved it
persists as facts on a per-source ``mapping`` Entity, additively (a changed
homes set leaves a ``was:`` trail, never a destructive overwrite).

RED because ``arcmemory.mapping`` does not exist yet (ModuleNotFoundError).
"""

from __future__ import annotations

from pathlib import Path

from arcstore.approvals import ApprovalStore
from arcstore.backends import open_backend

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.mapping import (
    approved_mapping,
    commit_mapping,
    load_committed_mapping,
    mapping_call_hash,
    stage_mapping_proposal,
)
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import SourceMapping

_AGENT_DID = "did:arc:mapping-test"
_OPERATOR_DID = "did:arc:operator"


def _approval_store(tmp_path: Path) -> ApprovalStore:
    backend = open_backend("sqlite", db_path=str(tmp_path / "approvals.db"))
    return ApprovalStore(backend)


# -- staging + approval gating (real ApprovalStore) --------------------------


async def test_stage_mapping_proposal_writes_a_pending_row(tmp_path: Path) -> None:
    store = _approval_store(tmp_path)
    proposal = SourceMapping(source_id="dropbox-1", homes=["memory", "document"])

    pending_id = await stage_mapping_proposal(
        proposal, approval_store=store, agent_did=_AGENT_DID
    )

    pending = await store.get(pending_id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.tool == "memory.map_source"
    assert pending.agent_did == _AGENT_DID
    assert pending.call_hash == mapping_call_hash("dropbox-1", ["memory", "document"])


async def test_approved_mapping_false_before_approval(tmp_path: Path) -> None:
    store = _approval_store(tmp_path)
    proposal = SourceMapping(source_id="wiki-2", homes=["document"])
    await stage_mapping_proposal(proposal, approval_store=store, agent_did=_AGENT_DID)

    assert await approved_mapping("wiki-2", ["document"], approval_store=store) is False


async def test_approved_mapping_true_after_operator_approves(tmp_path: Path) -> None:
    store = _approval_store(tmp_path)
    proposal = SourceMapping(source_id="wiki-3", homes=["document"])
    pending_id = await stage_mapping_proposal(
        proposal, approval_store=store, agent_did=_AGENT_DID
    )

    resolved = await store.resolve(
        pending_id, status="approved", actor_did=_OPERATOR_DID, grant=None
    )
    assert resolved is not None and resolved.status == "approved"

    assert await approved_mapping("wiki-3", ["document"], approval_store=store) is True


async def test_approved_mapping_fails_closed_when_denied(tmp_path: Path) -> None:
    store = _approval_store(tmp_path)
    proposal = SourceMapping(source_id="wiki-4", homes=["memory"])
    pending_id = await stage_mapping_proposal(
        proposal, approval_store=store, agent_did=_AGENT_DID
    )
    await store.resolve(pending_id, status="denied", actor_did=_OPERATOR_DID)

    assert await approved_mapping("wiki-4", ["memory"], approval_store=store) is False


async def test_approved_mapping_fails_closed_when_never_staged(tmp_path: Path) -> None:
    store = _approval_store(tmp_path)

    assert await approved_mapping("never-staged", ["memory"], approval_store=store) is False


# -- mapping-as-facts: commit + round-trip + additive was: trail -------------


def test_commit_mapping_then_load_round_trips(workspace: Path, db: MemoryDB) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=_AGENT_DID)
    mapping = SourceMapping(source_id="dropbox-5", homes=["memory"])

    commit_mapping(mapping, store=store)
    loaded = load_committed_mapping("dropbox-5", store=store)

    assert loaded is not None
    assert loaded.source_id == "dropbox-5"
    assert loaded.homes == ["memory"]


def test_load_committed_mapping_is_none_when_never_committed(
    workspace: Path, db: MemoryDB
) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=_AGENT_DID)

    assert load_committed_mapping("no-such-source", store=store) is None


def test_recommitting_a_changed_homes_set_leaves_a_was_trail(
    workspace: Path, db: MemoryDB
) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=_AGENT_DID)
    commit_mapping(SourceMapping(source_id="dropbox-6", homes=["memory"]), store=store)

    commit_mapping(
        SourceMapping(source_id="dropbox-6", homes=["document", "datastore"]), store=store
    )

    loaded = load_committed_mapping("dropbox-6", store=store)
    assert loaded is not None
    assert set(loaded.homes) == {"document", "datastore"}

    entity = store.read("mapping-dropbox-6")
    assert entity is not None
    fact = next(f for f in entity.facts if f.predicate == "homes")
    assert fact.was_value is not None and "memory" in fact.was_value
