"""H-047 — ``build_brain`` refuses a mismatched {workspace, agent_did, identity}.

``build_brain`` is arcagent's generic factory for an :class:`ArcMemoryBrain`. Before
this guard it read ``agent_did`` from the context dict verbatim and built a Brain for
it, while ``identity`` was independently optional — so an in-process caller could build
a Brain for ANOTHER agent's {workspace, agent_did} and read that agent's private memory
(ASI03 / LLM02; the cousin of the confirmed cross-agent memory bleed).

HONEST THREAT MODEL: in-process Python has no hard security boundary, so this guard is
not a wall against a deliberate in-process attacker. Its real job is to kill whole
classes of ACCIDENTAL cross-agent wiring bugs. Three cheap checks — claimed==proven,
proven-self-consistent, proven-owns-workspace — each catch a different accidental bug.

On any failure the guard raises :class:`~arcmemory.isolation.MemoryIsolationError` and
emits a ``memory.isolation_fault`` audit event — the same failure vocabulary the
runtime-resolution guard in ``arcagent.modules.memory._runtime`` uses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity

import arcmemory
from arcmemory import build_brain
from arcmemory.isolation import MemoryIsolationError


class _RecordingSink:
    """Captures every emitted audit event so faults can be asserted on."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="default", agent_type="executor")


def _context(
    workspace: Path,
    *,
    identity: Any,
    agent_did: str | None = None,
    audit_sink: Any = None,
    **backend: object,
) -> dict[str, Any]:
    did = agent_did if agent_did is not None else getattr(identity, "did", "")
    backend.setdefault("embed_backend", "none")
    return {
        "workspace": workspace,
        "agent_did": did,
        "tier": "personal",
        "audit_sink": audit_sink,
        "identity": identity,
        "policy_pipeline": None,
        "backend_config": dict(backend),
    }


def _seed_memory_scopes(workspace: Path, *scopes: str) -> None:
    """Write an index.db with one episodic row per given scope (no owner marker)."""
    db_path = workspace / "memory" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE episodic (event_id TEXT PRIMARY KEY, ts TEXT, scope TEXT, "
            "kind TEXT, text TEXT)"
        )
        for i, scope in enumerate(scopes):
            conn.execute(
                "INSERT INTO episodic VALUES (?, '2026-01-01', ?, 'tool', 'secret')",
                (f"e{i}", scope),
            )
        conn.commit()
    finally:
        conn.close()


def _fault_events(sink: _RecordingSink) -> list[AuditEvent]:
    return [e for e in sink.events if e.action == "memory.isolation_fault"]


# -- (e)/(f) the legit path + fresh bootstrap succeed --------------------------


def test_legit_matching_identity_and_workspace_builds_a_brain(tmp_path: Path) -> None:
    """Fresh workspace + matching identity: the guard binds ownership and builds."""
    identity = _identity()
    brain = build_brain(_context(tmp_path, identity=identity))
    assert isinstance(brain, arcmemory.ArcMemoryBrain)
    # First build adopts the workspace: the owner marker is now this identity's key.
    marker = tmp_path / "memory" / "owner.pub"
    assert marker.read_bytes() == identity.public_key


def test_rebuild_for_the_same_owner_succeeds(tmp_path: Path) -> None:
    """The owner may rebuild its own brain any number of times (marker matches)."""
    identity = _identity()
    build_brain(_context(tmp_path, identity=identity))
    brain = build_brain(_context(tmp_path, identity=identity))
    assert isinstance(brain, arcmemory.ArcMemoryBrain)


# -- (b) missing identity fails closed ----------------------------------------


def test_missing_identity_fails_closed(tmp_path: Path) -> None:
    sink = _RecordingSink()
    with pytest.raises(MemoryIsolationError):
        build_brain(_context(tmp_path, identity=None, audit_sink=sink))
    assert _fault_events(sink), "a memory.isolation_fault must be audited"


# -- (a) mismatched {workspace, agent_did} ------------------------------------


def test_agent_did_not_matching_the_identity_fails_closed(tmp_path: Path) -> None:
    """Right identity object, wrong agent_did string (check 1)."""
    identity = _identity()
    other = _identity()
    sink = _RecordingSink()
    with pytest.raises(MemoryIsolationError):
        build_brain(_context(tmp_path, identity=identity, agent_did=other.did, audit_sink=sink))
    assert _fault_events(sink)


def test_identity_on_a_workspace_owned_by_another_fails_closed(tmp_path: Path) -> None:
    """Right identity, wrong workspace path — the workspace is bound to another key."""
    owner = _identity()
    intruder = _identity()
    # Owner binds the workspace on its first build.
    build_brain(_context(tmp_path, identity=owner))
    sink = _RecordingSink()
    with pytest.raises(MemoryIsolationError):
        build_brain(_context(tmp_path, identity=intruder, audit_sink=sink))
    assert _fault_events(sink)


# -- (c) did != pubkey forgery ------------------------------------------------


def test_forged_did_not_matching_pubkey_fails_closed(tmp_path: Path) -> None:
    """A hand-built identity whose DID does not hash-match its public key (check 2)."""
    real = _identity()
    forged_did = "did:arc:default:executor/deadbeef"
    forged = AgentIdentity(did=forged_did, public_key=real.public_key)
    sink = _RecordingSink()
    with pytest.raises(MemoryIsolationError):
        build_brain(_context(tmp_path, identity=forged, audit_sink=sink))
    assert _fault_events(sink)


# -- (d) identity-less workspace WITH pre-existing (foreign) memory data -------


def test_foreign_owned_memory_data_without_marker_fails_closed(tmp_path: Path) -> None:
    """A workspace already holding ANOTHER agent's memory data must not be rebound.

    This is the victim-workspace shape: an owner marker is absent but the memory
    index records a different agent's scope. Adopting it would hand the intruder
    the victim's private recall, so the guard fails closed.
    """
    victim = _identity()
    intruder = _identity()
    _seed_memory_scopes(tmp_path, victim.did)
    sink = _RecordingSink()
    with pytest.raises(MemoryIsolationError):
        build_brain(_context(tmp_path, identity=intruder, audit_sink=sink))
    assert _fault_events(sink)


def test_mixed_own_plus_foreign_bleed_is_adopted(tmp_path: Path) -> None:
    """A CONTAMINATED OWN workspace — the builder's own data plus pre-existing foreign
    bleed rows (the known cross-agent-bleed bug) — is adopted, NOT bricked.

    build_brain binds the brain to THIS agent's scope; the foreign rows are never
    rebound and the read-time no-read-up scope gate keeps them out of recall. Failing
    the whole agent closed here (the old zero-tolerance rule) bricked every deployed
    agent that carried historical bleed. The victim shape — foreign data and NONE of
    the builder's — still fails closed (see the test above).
    """
    owner = _identity()
    stranger = _identity()
    _seed_memory_scopes(tmp_path, owner.did, owner.did, stranger.did)  # own + one foreign
    brain = build_brain(_context(tmp_path, identity=owner))
    assert isinstance(brain, arcmemory.ArcMemoryBrain)
    # Adopted for the owner; the foreign row is not rebound (marker is the owner's key).
    assert (tmp_path / "memory" / "owner.pub").read_bytes() == owner.public_key


def test_memory_data_with_no_attributable_rows_fails_closed(tmp_path: Path) -> None:
    """ZERO-TOLERANCE degenerate case: data files are present but NO scope-attributable
    rows exist (ownership cannot be established) — refused, not adopted."""
    owner = _identity()
    # A glass-box markdown file, but no index.db rows to attribute ownership from.
    entities = tmp_path / "memory" / "entities"
    entities.mkdir(parents=True)
    (entities / "vortex.md").write_text("# Vortex\nsecret deployment note\n", encoding="utf-8")
    sink = _RecordingSink()
    with pytest.raises(MemoryIsolationError):
        build_brain(_context(tmp_path, identity=owner, audit_sink=sink))
    assert _fault_events(sink)


def test_own_legacy_memory_data_without_marker_is_adopted(tmp_path: Path) -> None:
    """Non-regression: a pre-marker workspace whose data is ENTIRELY the builder's
    adopts on first build (the deployed fleet upgraded in place)."""
    owner = _identity()
    _seed_memory_scopes(tmp_path, owner.did, owner.did)  # every row owned by the builder
    brain = build_brain(_context(tmp_path, identity=owner))
    assert isinstance(brain, arcmemory.ArcMemoryBrain)
    assert (tmp_path / "memory" / "owner.pub").read_bytes() == owner.public_key
