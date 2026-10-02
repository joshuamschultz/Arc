"""Operator-signed local authority for scheduled and pulse control definitions.

The authority is what makes a schedule an approved artifact rather than a row in
an agent-writable JSON file. Every case below is an attack on that boundary: a
stale writer, a revoked artifact, a replayed occurrence, an edited journal, a
proof from a key that is not the actor's, and a replayed registration.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arctrust import (
    AgentIdentity,
    AuditEvent,
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
    InProcessSigner,
    LocalControlArtifactAuthority,
    LocalRunTriggerIssuer,
    SignedControlRevision,
    sign_control_actor_proof,
    verify_signature,
)
from arctrust.control import control_revision_message
from arctrust.identity import did_from_public_key

_TENANT = "arc-tenant"
_DEFINITION = b'{"prompt":"Send the morning briefing","type":"cron"}'


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def operator() -> InProcessSigner:
    return InProcessSigner(os.urandom(32))


@pytest.fixture
def agent() -> AgentIdentity:
    return AgentIdentity.generate(org="test", agent_type="executor")


@pytest.fixture
def sink() -> _Sink:
    return _Sink()


@pytest.fixture
def authority(
    tmp_path: Path, operator: InProcessSigner, agent: AgentIdentity, clock: _Clock, sink: _Sink
) -> LocalControlArtifactAuthority:
    built = LocalControlArtifactAuthority(
        tmp_path / "control",
        signer=operator,
        operator_did=did_from_public_key(operator.public_key, org="local", agent_type="operator"),
        clock=clock,
        audit_sink=sink,
    )
    built.enroll_actor(agent.did, agent.public_key, agent.algorithm)
    return built


def _proof(
    signer: AgentIdentity | InProcessSigner,
    actor_did: str,
    clock: _Clock,
    *,
    artifact_id: str = "sched-1",
    definition: bytes = _DEFINITION,
) -> bytes:
    return sign_control_actor_proof(
        signer,
        actor_did=actor_did,
        purpose="schedule",
        artifact_id=artifact_id,
        definition=definition,
        issued_at=clock(),
    )


async def _register(
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    clock: _Clock,
    *,
    expected: int | None = None,
    definition: bytes = _DEFINITION,
    proof: bytes | None = None,
) -> SignedControlRevision:
    return await authority.register_revision(
        tenant_id=_TENANT,
        agent_did=agent.did,
        purpose="schedule",
        artifact_id="sched-1",
        canonical_definition=definition,
        expected_revision=expected,
        actor_proof=proof or _proof(agent, agent.did, clock, definition=definition),
    )


async def _verify(
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    approval: SignedControlRevision,
    occurrence_id: str = "occ-1",
    definition: bytes = _DEFINITION,
) -> None:
    await authority.verify_current(
        tenant_id=_TENANT,
        agent_did=agent.did,
        purpose="schedule",
        artifact_id="sched-1",
        canonical_definition=definition,
        approval=approval,
        occurrence_id=occurrence_id,
    )


async def test_register_first_revision_and_verify_current(
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    operator: InProcessSigner,
    clock: _Clock,
    sink: _Sink,
) -> None:
    approval = await _register(authority, agent, clock)

    assert approval.revision == 1
    assert approval.tenant_id == _TENANT
    assert approval.agent_did == agent.did
    assert approval.actor_did == agent.did
    assert approval.definition_digest == hashlib.sha256(_DEFINITION).hexdigest()
    assert not approval.revoked
    assert verify_signature(
        operator.algorithm,
        control_revision_message(approval),
        bytes.fromhex(approval.signature),
        operator.public_key,
    )
    await _verify(authority, agent, approval)

    # The next revision supersedes the first: the old approval no longer fires.
    changed = _DEFINITION.replace(b"briefing", b"digest")
    second = await _register(authority, agent, clock, expected=1, definition=changed)
    assert second.revision == 2
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, approval, occurrence_id="occ-2")
    await _verify(authority, agent, second, occurrence_id="occ-3", definition=changed)
    assert {event.action for event in sink.events} >= {
        "control_artifact.register",
        "control_artifact.verify",
    }
    assert all(event.extra.get("custody") == "local_file" for event in sink.events)


async def test_stale_expected_revision_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    await _register(authority, agent, clock)
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, expected=None)
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, expected=7)


async def test_revoked_head_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    approval = await _register(authority, agent, clock)
    revoked = await authority.revoke(
        tenant_id=_TENANT, agent_did=agent.did, purpose="schedule", artifact_id="sched-1"
    )
    assert revoked.revoked and revoked.revision == 2

    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, approval)
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, revoked)
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, expected=2)


async def test_occurrence_replay_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    approval = await _register(authority, agent, clock)
    await _verify(authority, agent, approval, occurrence_id="occ-1")
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, approval, occurrence_id="occ-1")
    await _verify(authority, agent, approval, occurrence_id="occ-2")


async def test_tampered_journal_fails_closed(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    operator: InProcessSigner,
    clock: _Clock,
) -> None:
    approval = await _register(authority, agent, clock)
    journal = next((tmp_path / "control").glob("*.jsonl"))
    line = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    line["digest"] = "f" * 64
    journal.write_text(json.dumps(line) + "\n", encoding="utf-8")

    with pytest.raises(ControlArtifactUnavailableError):
        await _verify(authority, agent, approval)
    with pytest.raises(ControlArtifactUnavailableError):
        await _register(authority, agent, clock, expected=1)

    # A fresh process reading the edited journal fails closed the same way.
    fresh = LocalControlArtifactAuthority(
        tmp_path / "control",
        signer=operator,
        operator_did=did_from_public_key(operator.public_key, org="local", agent_type="operator"),
        clock=clock,
    )
    with pytest.raises(ControlArtifactUnavailableError):
        await _verify(fresh, agent, approval)


async def test_forged_approval_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    """An approval written into schedules.json by hand never matches the head."""
    approval = await _register(authority, agent, clock)
    other = _DEFINITION.replace(b"briefing", b"exfiltrate")
    forged = approval.model_copy(
        update={"definition_digest": hashlib.sha256(other).hexdigest(), "signature": "ab" * 64}
    )
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, forged, definition=other)
    resigned = approval.model_copy(update={"signature": "cd" * 64})
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, resigned)


async def test_actor_proof_from_wrong_key_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    impostor = AgentIdentity.generate(org="test", agent_type="executor")
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, proof=_proof(impostor, agent.did, clock))
    # An enrolled-elsewhere agent cannot act for this one either.
    authority.enroll_actor(impostor.did, impostor.public_key, impostor.algorithm)
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, proof=_proof(impostor, impostor.did, clock))
    # A proof for a different definition does not authorize this one.
    with pytest.raises(ControlArtifactRefusedError):
        await _register(
            authority,
            agent,
            clock,
            proof=_proof(agent, agent.did, clock, definition=b"{}"),
        )
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, proof=b"not a proof")


async def test_operator_proof_is_accepted_for_any_agent(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    proof = authority.operator_proof("schedule", "sched-1", _DEFINITION)
    approval = await _register(authority, agent, clock, proof=proof)
    assert approval.actor_did == authority.operator_did


async def test_replayed_registration_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    proof = _proof(agent, agent.did, clock)
    await _register(authority, agent, clock, proof=proof)
    await authority.revoke(
        tenant_id=_TENANT, agent_did=agent.did, purpose="schedule", artifact_id="sched-1"
    )
    # Same proof, presented again for a fresh artifact slot: a replay.
    with pytest.raises(ControlArtifactRefusedError):
        await authority.register_revision(
            tenant_id=_TENANT,
            agent_did=agent.did,
            purpose="schedule",
            artifact_id="sched-1",
            canonical_definition=_DEFINITION,
            expected_revision=2,
            actor_proof=proof,
        )


async def test_expired_actor_proof_refused(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity, clock: _Clock
) -> None:
    proof = _proof(agent, agent.did, clock)
    clock.now += timedelta(minutes=10)
    with pytest.raises(ControlArtifactRefusedError):
        await _register(authority, agent, clock, proof=proof)


def test_enrolled_actor_key_cannot_be_swapped(
    authority: LocalControlArtifactAuthority, agent: AgentIdentity
) -> None:
    authority.enroll_actor(agent.did, agent.public_key, agent.algorithm)
    other = AgentIdentity.generate(org="test", agent_type="executor")
    with pytest.raises(ControlArtifactRefusedError):
        authority.enroll_actor(agent.did, other.public_key, other.algorithm)
    with pytest.raises(ControlArtifactRefusedError):
        authority.enroll_actor(authority.operator_did, other.public_key, other.algorithm)


async def test_run_trigger_issuer_signs_request_and_evidence(
    operator: InProcessSigner, clock: _Clock
) -> None:
    class _Request:
        def digest(self) -> str:
            return "a" * 64

    issuer = LocalRunTriggerIssuer(operator, clock=clock, ttl=timedelta(minutes=30))
    authorization, deadline = await issuer(_Request(), b"evidence")
    assert deadline == clock.now + timedelta(minutes=30)
    body = json.loads(authorization)
    assert body["request_digest"] == "a" * 64
    assert body["evidence_digest"] == hashlib.sha256(b"evidence").hexdigest()
    unsigned = {key: value for key, value in body.items() if key != "signature"}
    assert verify_signature(
        operator.algorithm,
        b"arc.local-run-trigger.v1\n"
        + json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
        bytes.fromhex(body["signature"]),
        operator.public_key,
    )


# ----------------------------------------------- persisted occurrence replay


def _fresh(
    tmp_path: Path, operator: InProcessSigner, clock: _Clock, **kwargs: int
) -> LocalControlArtifactAuthority:
    return LocalControlArtifactAuthority(
        tmp_path / "control",
        signer=operator,
        operator_did=did_from_public_key(operator.public_key, org="local", agent_type="operator"),
        clock=clock,
        **kwargs,
    )


def _occurrence_file(tmp_path: Path) -> Path:
    return next((tmp_path / "control").glob("*.occurrences.jsonl"))


async def test_occurrence_replay_refused_across_process_restart(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    operator: InProcessSigner,
    clock: _Clock,
) -> None:
    approval = await _register(authority, agent, clock)
    await _verify(authority, agent, approval, occurrence_id="occ-1")

    restarted = _fresh(tmp_path, operator, clock)
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(restarted, agent, approval, occurrence_id="occ-1")
    await _verify(restarted, agent, approval, occurrence_id="occ-2")


async def test_occurrence_log_is_per_target(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    clock: _Clock,
) -> None:
    approval = await _register(authority, agent, clock)
    await _verify(authority, agent, approval, occurrence_id="occ-1")
    assert len(_occurrence_file(tmp_path).read_text(encoding="utf-8").splitlines()) == 1
    assert "occ-1" not in _occurrence_file(tmp_path).read_text(encoding="utf-8")


async def test_torn_last_occurrence_line_is_not_an_admission_and_not_a_crash(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    operator: InProcessSigner,
    clock: _Clock,
) -> None:
    approval = await _register(authority, agent, clock)
    await _verify(authority, agent, approval, occurrence_id="occ-1")
    log = _occurrence_file(tmp_path)
    admitted = log.read_text(encoding="utf-8")
    # A crash mid-append left half a digest with no newline.
    log.write_text(admitted + hashlib.sha256(b"x").hexdigest()[:20], encoding="utf-8")

    restarted = _fresh(tmp_path, operator, clock)
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(restarted, agent, approval, occurrence_id="occ-1")
    await _verify(restarted, agent, approval, occurrence_id="occ-2")
    # The torn fragment never merged with the new entry: both digests parse.
    lines = log.read_text(encoding="utf-8").splitlines()
    assert admitted.strip() in lines
    assert sum(1 for line in lines if len(line) == 64) == 2


async def test_unterminated_complete_digest_is_not_read_as_admission(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    operator: InProcessSigner,
    clock: _Clock,
) -> None:
    approval = await _register(authority, agent, clock)
    await _verify(authority, agent, approval, occurrence_id="occ-1")
    log = _occurrence_file(tmp_path)
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

    restarted = _fresh(tmp_path, operator, clock)
    await _verify(restarted, agent, approval, occurrence_id="occ-1")


async def test_occurrence_log_is_bounded(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    operator: InProcessSigner,
    clock: _Clock,
) -> None:
    bounded = _fresh(tmp_path, operator, clock, max_remembered=10)
    bounded.enroll_actor(agent.did, agent.public_key, agent.algorithm)
    approval = await _register(bounded, agent, clock)
    for number in range(35):
        await _verify(bounded, agent, approval, occurrence_id=f"occ-{number}")
    assert len(_occurrence_file(tmp_path).read_text(encoding="utf-8").splitlines()) <= 10
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(bounded, agent, approval, occurrence_id="occ-34")


async def test_planted_junk_in_occurrence_log_is_ignored(
    tmp_path: Path,
    authority: LocalControlArtifactAuthority,
    agent: AgentIdentity,
    clock: _Clock,
) -> None:
    approval = await _register(authority, agent, clock)
    await _verify(authority, agent, approval, occurrence_id="occ-1")
    log = _occurrence_file(tmp_path)
    log.write_text("not-a-digest\n{}\n\n" + log.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ControlArtifactRefusedError):
        await _verify(authority, agent, approval, occurrence_id="occ-1")
    await _verify(authority, agent, approval, occurrence_id="occ-2")
