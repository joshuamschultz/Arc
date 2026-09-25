"""The remote queue root and recovery fence share one broker authority."""

import base64
import hashlib
import json
import time

import httpx
import pytest

from arctrust import (
    ED25519,
    BrokerQueueByteCipher,
    BrokerQueueLease,
    BrokerQueueRecoveryProof,
    InProcessSigner,
    QueueBrokerAnchor,
    QueueBrokerError,
    canonical_json,
    sign_broker_queue_lease,
    sign_broker_queue_recovery,
    verify_signature,
)


def _json_response(body: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        stream=httpx.ByteStream(canonical_json(body)),
    )


def _lease(machine: InProcessSigner, now: int) -> BrokerQueueLease:
    return BrokerQueueLease(
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        machine_id="machine-a",
        machine_public_key=machine.public_key.hex(),
        tls_fingerprint="c" * 64,
        lease_id="lease-abc",
        active_owner_epoch="2",
        fenced_through_epoch="1",
        issued_at=now - 1,
        expires_at=now + 60,
        next_sequence=1,
        allowed_purposes=(
            "anchor.read",
            "anchor.advance",
            "queue.recover",
            "record.seal",
            "record.open",
        ),
    )


def test_queue_broker_refuses_unsigned_or_unscoped_lease() -> None:
    signer = InProcessSigner(b"a" * 32)
    broker = InProcessSigner(b"b" * 32)
    facts = BrokerQueueLease(
        version=1,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        machine_id="machine-a",
        machine_public_key=signer.public_key.hex(),
        tls_fingerprint="c" * 64,
        lease_id="lease-abc",
        active_owner_epoch="2",
        fenced_through_epoch="1",
        issued_at=int(time.time()) - 1,
        expires_at=int(time.time()) + 60,
        next_sequence=1,
        allowed_purposes=(
            "anchor.read",
            "anchor.advance",
            "queue.recover",
            "record.seal",
            "record.open",
        ),
    )
    envelope = sign_broker_queue_lease(facts, broker.sign)
    client = httpx.Client(base_url="https://broker.example")
    with pytest.raises(QueueBrokerError, match="lease"):
        QueueBrokerAnchor(
            client,
            tenant_id="tenant-a",
            journal_scope="queue/tenant-a",
            machine_id="machine-a",
            tls_fingerprint="c" * 64,
            signer=signer,
            broker_public_key=broker.public_key,
            lease_envelope={
                "facts": {**envelope["facts"], "tenant_id": "tenant-b"},
                "signature": envelope["signature"],
            },
        )


@pytest.mark.parametrize("lose_response", [False, True])
def test_broker_anchor_signs_exact_cas_and_reconciles_loss(lose_response: bool) -> None:
    now = 1000
    machine = InProcessSigner(b"a" * 32)
    broker = InProcessSigner(b"b" * 32)
    lease = _lease(machine, now)
    head: dict[str, object] | None = None
    sequence = 1

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal head, sequence
        operation = json.loads(
            base64.urlsafe_b64decode(request.headers["X-Arc-Machine-Operation"] + "=" * 3)
        )
        signature = bytes.fromhex(operation.pop("signature"))
        assert verify_signature(
            ED25519,
            b"arc:machine-broker-operation:v1\0" + canonical_json(operation),
            signature,
            machine.public_key,
        )
        assert operation["lease_epoch"] == 2
        assert operation["method"] == request.method
        assert operation["path"] == request.url.path
        if request.method == "GET":
            assert operation["sequence"] == 0
            query = dict(request.url.params)
            expected_digest = hashlib.sha256(canonical_json(query)).hexdigest()
            assert operation["body_sha256"] == expected_digest
        else:
            assert operation["sequence"] == sequence
            assert operation["body_sha256"] == hashlib.sha256(request.content).hexdigest()
            body = json.loads(request.content)
            assert body["expected_head"] == head
            head = {
                "scope": "queue/tenant-a",
                "version": 1,
                "digest": body["digest"],
                "previous_digest": None,
                "intent": body["intent"],
            }
            sequence += 1
        signed = sign_broker_queue_lease(
            lease.model_copy(update={"next_sequence": sequence}), broker.sign
        )
        if request.method == "POST" and lose_response:
            raise httpx.ReadError("lost response")
        return _json_response({"head": head, "lease": signed})

    client = httpx.Client(
        base_url="https://broker.example", transport=httpx.MockTransport(handler)
    )
    anchor = QueueBrokerAnchor(
        client,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        machine_id="machine-a",
        tls_fingerprint="c" * 64,
        signer=machine,
        broker_public_key=broker.public_key,
        lease_envelope=sign_broker_queue_lease(lease, broker.sign),
        clock=lambda: now,
    )
    assert anchor.latest() is None
    advanced = anchor.compare_and_advance(None, "d" * 64, "intent")
    assert advanced.digest == "d" * 64
    assert advanced.version == 1
    assert anchor.latest() == advanced


def test_recovery_uses_broker_refreshed_proof_and_fenced_cas() -> None:
    now = 1000
    machine = InProcessSigner(b"a" * 32)
    broker = InProcessSigner(b"b" * 32)
    lease = _lease(machine, now)
    old_head = {
        "scope": "queue/tenant-a",
        "version": 1,
        "digest": "a" * 64,
        "previous_digest": None,
        "intent": "accepted",
    }
    proof_facts = BrokerQueueRecoveryProof(
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        prior_owner_epoch="1",
        active_owner_epoch="2",
        lease_id="lease-abc",
        nonce="x" * 32,
        issued_at=now - 1,
        expires_at=now + 58,
    )
    initial_proof = sign_broker_queue_recovery(proof_facts, broker.sign)
    refreshed_proof = sign_broker_queue_recovery(
        proof_facts.model_copy(update={"nonce": "y" * 32}), broker.sign
    )
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params.get("prior_owner_epoch") == "1"
            return _json_response(
                {
                    "head": old_head,
                    "lease": sign_broker_queue_lease(lease, broker.sign),
                    "recovery_proof": refreshed_proof,
                },
            )
        sent.append(json.loads(request.content))
        assert request.url.path == "/broker/queue/recover-cas"
        assert sent[0]["recovery_proof"] == refreshed_proof
        return _json_response(
            {
                "head": {
                    "scope": "queue/tenant-a",
                    "version": 2,
                    "digest": "d" * 64,
                    "previous_digest": "a" * 64,
                    "intent": "recovered",
                },
                "lease": sign_broker_queue_lease(
                    lease.model_copy(update={"next_sequence": 2}), broker.sign
                ),
                "recovery_proof": refreshed_proof,
            },
        )

    client = httpx.Client(
        base_url="https://broker.example", transport=httpx.MockTransport(handler)
    )
    anchor = QueueBrokerAnchor(
        client,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        machine_id="machine-a",
        tls_fingerprint="c" * 64,
        signer=machine,
        broker_public_key=broker.public_key,
        lease_envelope=sign_broker_queue_lease(lease, broker.sign),
        clock=lambda: now,
    )
    original = canonical_json(initial_proof).decode()
    with pytest.raises(QueueBrokerError, match="proof"):
        anchor.validate(
            original,
            journal_scope="queue/tenant-a",
            tenant_id="tenant-a",
            owner_epoch="2",
            purpose="queue.recover",
        )
    assert json.loads(anchor.recovery_proof("1")) == refreshed_proof
    anchor.validate(
        original,
        journal_scope="queue/tenant-a",
        tenant_id="tenant-a",
        owner_epoch="1",
        purpose="queue.recover",
    )
    from arctrust import AnchorHead

    advanced = anchor.compare_and_advance(
        AnchorHead.model_validate(old_head),
        "d" * 64,
        "recovered",
        original,
        journal_scope="queue/tenant-a",
        tenant_id="tenant-a",
        owner_epoch="1",
        purpose="queue.recover",
    )
    assert advanced.version == 2
    assert sent[0]["prior_owner_epoch"] == "1"


@pytest.mark.parametrize("kind", ["oversized", "redirect", "compressed"])
def test_broker_refuses_unbounded_or_redirected_response(kind: str) -> None:
    now = 1000
    machine = InProcessSigner(b"a" * 32)
    broker = InProcessSigner(b"b" * 32)
    lease = _lease(machine, now)
    seen = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.example/"})
        if kind == "compressed":
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx.ByteStream(b"not-a-safe-compressed-payload"),
            )
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 17_000))

    client = httpx.Client(
        base_url="https://broker.example", transport=httpx.MockTransport(handler)
    )
    anchor = QueueBrokerAnchor(
        client,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        machine_id="machine-a",
        tls_fingerprint="c" * 64,
        signer=machine,
        broker_public_key=broker.public_key,
        lease_envelope=sign_broker_queue_lease(lease, broker.sign),
        clock=lambda: now,
    )
    with pytest.raises(QueueBrokerError):
        anchor.latest()
    assert seen == 1


def test_record_cipher_uses_same_lease_sequence_as_queue_anchor() -> None:
    now = 1000
    machine = InProcessSigner(b"a" * 32)
    broker = InProcessSigner(b"b" * 32)
    lease = _lease(machine, now)
    sequence = 1
    head: dict[str, object] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sequence, head
        operation = json.loads(
            base64.urlsafe_b64decode(request.headers["X-Arc-Machine-Operation"] + "=" * 3)
        )
        if request.method == "GET":
            assert operation["sequence"] == 0
            return _json_response(
                {
                    "head": head,
                    "lease": sign_broker_queue_lease(
                        lease.model_copy(update={"next_sequence": sequence}), broker.sign
                    ),
                }
            )
        assert operation["sequence"] == sequence
        body = json.loads(request.content)
        if request.url.path == "/broker/queue/cas":
            assert operation["purpose"] == "anchor.advance"
            head = {
                "scope": "queue/tenant-a",
                "version": 1,
                "digest": body["digest"],
                "previous_digest": None,
                "intent": body["intent"],
            }
            result: dict[str, object] = {"head": head}
        elif request.url.path.endswith("/seal"):
            assert body["purpose"] == "queue.record"
            assert operation["purpose"] == "record.seal"
            result = {"ciphertext": "vault:v1:sealed"}
        else:
            assert body["purpose"] == "queue.record"
            assert operation["purpose"] == "record.open"
            result = {"plaintext": base64.urlsafe_b64encode(b"record").decode().rstrip("=")}
        sequence += 1
        return _json_response(
            {
                **result,
                "lease": sign_broker_queue_lease(
                    lease.model_copy(update={"next_sequence": sequence}), broker.sign
                ),
            }
        )

    client = httpx.Client(
        base_url="https://broker.example", transport=httpx.MockTransport(handler)
    )
    anchor = QueueBrokerAnchor(
        client,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        machine_id="machine-a",
        tls_fingerprint="c" * 64,
        signer=machine,
        broker_public_key=broker.public_key,
        lease_envelope=sign_broker_queue_lease(lease, broker.sign),
        clock=lambda: now,
    )
    cipher = BrokerQueueByteCipher(anchor)
    assert cipher.seal(b"record") == "vault:v1:sealed"
    assert cipher.open("vault:v1:sealed") == b"record"
    assert anchor.compare_and_advance(None, "d" * 64, "accepted").version == 1
    assert sequence == 4
