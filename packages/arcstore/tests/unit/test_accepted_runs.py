"""Accepted agent-run persistence contract over ArcStore's mutable plane."""

from datetime import UTC, datetime

import arctrust
import pytest
from pydantic import ValidationError

from arcstore import AcceptedRunStore, RunBlobRef, StoredRunIntent
from arcstore.accepted_runs import _key
from arcstore.backends.memory import FakeBackend


@pytest.fixture
def store() -> AcceptedRunStore:
    return AcceptedRunStore(
        FakeBackend(),
        arctrust.RecordCipher(bytes(range(32))),
        authorize=lambda action, tenant, agent: bool(action and tenant and agent),
        audit_sink=RecordingAudit(),
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[arctrust.AuditEvent] = []

    def write_durable(self, event: arctrust.AuditEvent) -> None:
        self.events.append(event)


async def test_encrypted_request_roundtrip_and_scope(store: AcceptedRunStore) -> None:
    ref = await store.write_blob(
        "tenant-a", "did:arc:agent/1", b"secret request", purpose="request"
    )
    assert isinstance(ref, RunBlobRef)
    assert await store.read_blob("tenant-a", "did:arc:agent/1", ref) == b"secret request"
    with pytest.raises(ValueError):
        await store.read_blob("tenant-b", "did:arc:agent/1", ref)


async def test_versioned_intent_transition_refuses_stale_writer(store: AcceptedRunStore) -> None:
    request = await store.write_blob("tenant-a", "did:arc:agent/1", b"request", purpose="request")
    staging = StoredRunIntent(
        run_id="run-1",
        tenant_id="tenant-a",
        agent_did="did:arc:agent/1",
        session_id="session-1",
        purpose="manual",
        occurrence_id="run-1",
        authorization_nonce="nonce-1",
        owner_epoch=1,
        version=1,
        status="staging",
        deadline=datetime.now(UTC),
        authorization_digest="a" * 64,
        request_digest="b" * 64,
        request_ref=request,
        reserved_bytes=100,
    )
    assert await store.create(staging) == staging
    accepted = staging.model_copy(update={"status": "accepted", "version": 2})
    assert await store.compare_and_set(staging, accepted) is True
    assert await store.compare_and_set(staging, accepted) is False
    assert await store.get("tenant-a", "did:arc:agent/1", "run-1") == accepted


async def test_ciphertext_cannot_be_relabelled_to_another_scope() -> None:
    backend = FakeBackend()
    store = AcceptedRunStore(
        backend,
        arctrust.RecordCipher(bytes(range(32))),
        authorize=lambda action, tenant, agent: True,
        audit_sink=RecordingAudit(),
    )
    ref = await store.write_blob("tenant-a", "did:arc:agent/1", b"same", purpose="request")
    original = await backend.mutable_read(
        "accepted_run_blobs", _key("tenant-a", "did:arc:agent/1", "request", ref.sha256)
    )
    assert original is not None
    await backend.mutable_write(
        "accepted_run_blobs",
        _key("tenant-b", "did:arc:agent/2", "request", ref.sha256),
        {**original, "tenant_id": "tenant-b", "agent_did": "did:arc:agent/2"},
        actor_did="did:arc:agent/2",
    )
    with pytest.raises(ValueError, match="sealed scope"):
        await store.read_blob("tenant-b", "did:arc:agent/2", ref)


async def test_denied_or_failed_audit_stops_protected_read() -> None:
    backend = FakeBackend()
    audit = RecordingAudit()
    denied = AcceptedRunStore(
        backend,
        arctrust.RecordCipher(bytes(range(32))),
        authorize=lambda action, tenant, agent: False,
        audit_sink=audit,
    )
    with pytest.raises(PermissionError):
        await denied.get("tenant", "agent", "run")
    assert audit.events[-1].outcome == "deny"

    class BrokenAudit:
        def write_durable(self, event: arctrust.AuditEvent) -> None:
            raise RuntimeError("audit unavailable")

    broken = AcceptedRunStore(
        backend,
        arctrust.RecordCipher(bytes(range(32))),
        authorize=lambda action, tenant, agent: True,
        audit_sink=BrokenAudit(),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await broken.get("tenant", "agent", "run")


def test_naive_deadline_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        StoredRunIntent(
            run_id="run-1",
            tenant_id="tenant",
            agent_did="agent",
            session_id="session",
            purpose="manual",
            occurrence_id="run-1",
            authorization_nonce="nonce-1",
            owner_epoch=1,
            version=1,
            status="staging",
            deadline=datetime(2030, 1, 1),
            authorization_digest="a" * 64,
            request_digest="b" * 64,
            reserved_bytes=1,
        )
