"""Authenticated accepted-run reservation and uncertain-outcome recovery."""

import hashlib
import threading
from datetime import UTC, datetime
from typing import Any

import arcstore
import arctrust
import pytest
from arcstore.accepted_runs import _key
from arcstore.backends.memory import FakeBackend

from arcagent.modules.run_intents.ledger import (
    RunIntentLedger,
    RunIntentUnavailableError,
    VerifiedRunAuthorization,
    _scope,
)

_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


def verified(evidence: bytes) -> VerifiedRunAuthorization:
    if evidence != b"signed:request":
        raise ValueError("signature invalid")
    return VerifiedRunAuthorization(
        tenant_id="tenant",
        agent_did="did:arc:agent/1",
        run_id="run-1",
        session_id="session-1",
        owner_epoch=1,
        request_digest=hashlib.sha256(b"request body").hexdigest(),
        deadline=_DEADLINE,
        purpose="manual",
        occurrence_id="run-1",
        nonce="nonce-1",
    )


class RecordingAudit:
    def write_durable(self, event: arctrust.AuditEvent) -> None:
        del event


class Anchor:
    def __init__(self) -> None:
        self.scope = _scope("tenant", "did:arc:agent/1")
        self.head: arctrust.AnchorHead | None = None

    def latest(self) -> arctrust.AnchorHead | None:
        return self.head

    def compare_and_advance(
        self, expected: arctrust.AnchorHead | None, digest: str, intent: str
    ) -> arctrust.AnchorHead:
        if self.head != expected:
            raise RuntimeError("anchor CAS conflict")
        self.head = arctrust.AnchorHead(
            scope=self.scope,
            version=1 if expected is None else expected.version + 1,
            digest=digest,
            previous_digest=None if expected is None else expected.digest,
            intent=intent,
        )
        return self.head


def make_ledger(
    backend: FakeBackend,
    anchor: Anchor,
    *,
    max_reserved_bytes: int = 1024,
    anchor_timeout_seconds: float = 5.0,
    max_blob_bytes: int = 1_048_576,
) -> RunIntentLedger:
    return RunIntentLedger(
        tenant_id="tenant",
        agent_did="did:arc:agent/1",
        store=arcstore.AcceptedRunStore(
            backend,
            arctrust.RecordCipher(bytes(range(32))),
            authorize=lambda action, tenant, agent: bool(action and tenant and agent),
            audit_sink=RecordingAudit(),
            max_blob_bytes=max_blob_bytes,
        ),
        anchor=anchor,
        verify_authorization=verified,
        owner_epoch=1,
        max_reserved_bytes=max_reserved_bytes,
        anchor_timeout_seconds=anchor_timeout_seconds,
    )


async def accept(ledger: RunIntentLedger, run_id: str = "run-1") -> arcstore.StoredRunIntent:
    return await ledger.accept(
        run_id=run_id,
        session_id="session-1",
        owner_epoch=1,
        deadline=_DEADLINE,
        request=b"request body",
        signed_authorization=b"signed:request",
        max_result_bytes=100,
        purpose="manual",
        occurrence_id=run_id,
    )


async def test_accepted_result_roundtrip_and_no_second_effect() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    accepted = await accept(ledger)
    calls = 0

    async def effect(request: bytes) -> bytes:
        nonlocal calls
        calls += 1
        assert request == b"request body"
        return b"completed result"

    completed = await ledger.execute(accepted, effect)
    assert completed.status == "completed"
    assert completed.result_ref is not None
    assert (
        await ledger._store.read_blob("tenant", "did:arc:agent/1", completed.result_ref)
        == b"completed result"
    )
    with pytest.raises(RunIntentUnavailableError):
        await ledger.execute(accepted, effect)
    assert calls == 1


async def test_recover_after_anchored_acceptance_and_lost_store_cas() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    original = ledger._store.compare_and_set
    lost = False

    async def lose_once(*args: Any, **kwargs: Any) -> bool:
        nonlocal lost
        if not lost:
            lost = True
            raise RuntimeError("DB response lost before CAS")
        return await original(*args, **kwargs)

    ledger._store.compare_and_set = lose_once  # type: ignore[method-assign]
    with pytest.raises(RunIntentUnavailableError):
        await accept(ledger)
    recovered = make_ledger(backend, anchor)
    items = await recovered.recover()
    assert len(items) == 1 and items[0].status == "accepted"


async def test_recover_executing_run_as_unknown_without_replaying_effect() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    accepted = await accept(ledger)
    started = False

    async def effect(_: bytes) -> bytes:
        nonlocal started
        started = True
        raise RuntimeError("provider result lost")

    with pytest.raises(RuntimeError):
        await ledger.execute(accepted, effect)
    assert started
    recovered = make_ledger(backend, anchor)
    items = await recovered.recover()
    assert len(items) == 1 and items[0].status == "outcome_unknown"
    with pytest.raises(RunIntentUnavailableError):
        await recovered.execute(accepted, effect)


async def test_tampered_prior_projection_is_not_reconciled() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    accepted = await accept(ledger)
    key = _key("tenant", "did:arc:agent/1", "intent", "run-1")
    await backend.mutable_merge(
        "accepted_run_intents", key, {"session_id": "stolen"}, actor_did="did:arc:agent/1"
    )
    with pytest.raises(RunIntentUnavailableError):
        await ledger.get(accepted.run_id)


async def test_capacity_refusal_happens_before_blob_write() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor, max_reserved_bytes=10)
    with pytest.raises(RunIntentUnavailableError, match="capacity"):
        await accept(ledger)
    assert await backend.mutable_query("accepted_run_blobs", where={}) == []


async def test_redelivery_resumes_staging_without_second_reservation() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    original = ledger._store.write_blob
    lost = False

    async def lose_once(*args: Any, **kwargs: Any) -> arcstore.RunBlobRef:
        nonlocal lost
        if not lost:
            lost = True
            raise RuntimeError("blob write response lost")
        return await original(*args, **kwargs)

    ledger._store.write_blob = lose_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await accept(ledger)
    version = anchor.head.version if anchor.head else 0
    recovered = make_ledger(backend, anchor)
    accepted = await accept(recovered)
    assert accepted.status == "accepted" and accepted.version == 2
    assert anchor.head is not None and anchor.head.version == version + 1
    assert await accept(recovered) == accepted


async def test_result_cannot_exceed_pre_effect_reservation() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    accepted = await accept(ledger)

    async def oversized(_: bytes) -> bytes:
        return b"x" * 101

    with pytest.raises(RunIntentUnavailableError, match="reserved capacity"):
        await ledger.execute(accepted, oversized)
    assert (await ledger.get("run-1")).status == "outcome_unknown"


async def test_redelivery_cannot_change_reserved_deadline() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    await accept(ledger)
    with pytest.raises(RunIntentUnavailableError, match="binding refused"):
        await ledger.accept(
            run_id="run-1",
            session_id="session-1",
            owner_epoch=1,
            deadline=datetime(2030, 1, 2, tzinfo=UTC),
            request=b"request body",
            signed_authorization=b"signed:request",
            max_result_bytes=100,
            purpose="manual",
            occurrence_id="run-1",
        )


@pytest.mark.parametrize(
    ("changes"),
    [
        {"run_id": "run-2", "occurrence_id": "run-2"},
        {"session_id": "other-session"},
        {"request": b"other request"},
        {"purpose": "schedule"},
    ],
)
async def test_signed_trigger_cannot_authorize_substituted_work(changes: dict[str, Any]) -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor)
    inputs: dict[str, Any] = {
        "run_id": "run-1",
        "session_id": "session-1",
        "owner_epoch": 1,
        "deadline": _DEADLINE,
        "request": b"request body",
        "signed_authorization": b"signed:request",
        "max_result_bytes": 100,
        "purpose": "manual",
        "occurrence_id": "run-1",
    }
    inputs.update(changes)
    with pytest.raises(RunIntentUnavailableError, match="binding refused"):
        await ledger.accept(**inputs)
    assert anchor.head is None
    assert await backend.mutable_query("accepted_run_intents", where={}) == []


async def test_blob_limit_is_checked_before_reserving_manifest() -> None:
    backend = FakeBackend()
    anchor = Anchor()
    ledger = make_ledger(backend, anchor, max_blob_bytes=10)
    with pytest.raises(ValueError, match="invalid run admission"):
        await accept(ledger)
    assert anchor.head is None


async def test_late_anchor_commit_is_drained_before_recovery() -> None:
    class SlowAnchor(Anchor):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def compare_and_advance(
            self, expected: arctrust.AnchorHead | None, digest: str, intent: str
        ) -> arctrust.AnchorHead:
            self.release.wait(timeout=2)
            return super().compare_and_advance(expected, digest, intent)

    backend = FakeBackend()
    anchor = SlowAnchor()
    ledger = make_ledger(backend, anchor, anchor_timeout_seconds=0.01)
    with pytest.raises(RunIntentUnavailableError, match="anchor advancement unavailable"):
        await accept(ledger)
    with pytest.raises(RunIntentUnavailableError, match="prior anchor call remains unresolved"):
        await ledger.recover()
    anchor.release.set()
    recovered = await ledger.recover()
    assert len(recovered) == 1 and recovered[0].status == "staging"

