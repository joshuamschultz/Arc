"""D-577 — audit records are encrypted at rest, and the chain still verifies.

SPEC-062 D-552 chose FULL capture, so the WORM chain now holds every email body
and document the fleet reads. These tests pin the four properties that make
sealing it safe:

* the captured content is not readable in the file on disk,
* :func:`~arctrust.audit.verify_chain` still passes over a sealed chain, and
  passes **without the sealing key** — integrity is provable by someone who may
  not read the content,
* a byte flipped inside the sealed payload still fails verification, so sealing
  did not buy tamper-evidence away,
* the envelope (who, what, when, outcome) stays in the clear, because the
  operational readers of this chain index on it and never on the content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctrust.audit import AuditEvent, WormSink, emit, read_verified_anchor, verify_chain
from arctrust.audit_cipher import RecordCipher, derive_record_key
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

_BODY = "the quarterly numbers are attached, do not forward"
_SEED = bytes(range(32))


@pytest.fixture
def cipher() -> RecordCipher:
    return RecordCipher(derive_record_key(_SEED))


def _connector_event() -> AuditEvent:
    """One full-capture connector record — the shape D-552 made high-value."""
    return AuditEvent(
        actor_did="did:arc:test:exec/aabbccdd",
        action="connector.call",
        target="connector:gmail_work:read_message",
        outcome="allow",
        extra={"arguments": {"id": "18f"}, "result": _BODY},
    )


class TestSealedAtRest:
    def test_captured_content_is_not_readable_on_disk(
        self, tmp_path: Path, cipher: RecordCipher
    ) -> None:
        """The body the connector read must not appear in the file's bytes."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)

        emit(_connector_event(), sink)

        assert _BODY not in path.read_text()

    def test_envelope_stays_readable(self, tmp_path: Path, cipher: RecordCipher) -> None:
        """Who/what/when/outcome stay in the clear — the store ingest indexes on them."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)

        emit(_connector_event(), sink)

        event = json.loads(path.read_text().splitlines()[0])["event"]
        assert event["actor_did"] == "did:arc:test:exec/aabbccdd"
        assert event["action"] == "connector.call"
        assert event["target"] == "connector:gmail_work:read_message"
        assert event["outcome"] == "allow"
        assert event["ts"]

    def test_unseal_round_trips_the_content(self, tmp_path: Path, cipher: RecordCipher) -> None:
        """The holder of the key reconstructs exactly what was captured (AU-3)."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)

        emit(_connector_event(), sink)

        event = json.loads(path.read_text().splitlines()[0])["event"]
        assert cipher.unseal(event)["extra"] == {
            "arguments": {"id": "18f"},
            "result": _BODY,
        }

    def test_a_wrong_key_cannot_unseal(self, tmp_path: Path, cipher: RecordCipher) -> None:
        """Sealing is real encryption, not encoding."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)
        emit(_connector_event(), sink)

        event = json.loads(path.read_text().splitlines()[0])["event"]
        other = RecordCipher(derive_record_key(bytes(32)))

        with pytest.raises(ValueError):
            other.unseal(event)


class TestChainStillVerifies:
    def test_sealed_chain_verifies(self, tmp_path: Path, cipher: RecordCipher) -> None:
        """Encryption must not cost the tamper-evident guarantee."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)
        emit(_connector_event(), sink)
        emit(_connector_event(), sink)

        assert sink.verify_chain() is True

    def test_verification_needs_no_sealing_key(self, tmp_path: Path, cipher: RecordCipher) -> None:
        """An auditor can prove integrity without being able to read the content."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)
        emit(_connector_event(), sink)

        assert verify_chain(path, kp.public_key) is True

    def test_tampering_inside_the_sealed_payload_is_detected(
        self, tmp_path: Path, cipher: RecordCipher
    ) -> None:
        """A flipped byte in the ciphertext still breaks the hash chain."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)
        emit(_connector_event(), sink)

        record = json.loads(path.read_text().splitlines()[0])
        sealed = record["event"]["extra"]
        (key,) = sealed
        sealed[key] = "A" + sealed[key][1:]
        path.write_text(json.dumps(record) + "\n")

        assert verify_chain(path, kp.public_key) is False

    def test_restart_resumes_a_sealed_chain_without_the_key(
        self, tmp_path: Path, cipher: RecordCipher
    ) -> None:
        """Tip recovery reads the envelope only, so a keyless restart still chains."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        first = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)
        emit(_connector_event(), first)
        first.close()

        second = WormSink(path, InProcessSigner(kp.private_key))
        emit(_connector_event(), second)

        assert verify_chain(path, kp.public_key) is True
        assert [json.loads(line)["seq"] for line in path.read_text().splitlines()] == [0, 1]


class TestAnchorReading:
    def test_anchor_is_unsealed_for_a_key_holder(
        self, tmp_path: Path, cipher: RecordCipher
    ) -> None:
        """The checkpoint manifest lives in ``extra``, so the reader needs the key."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key), cipher=cipher)
        emit(
            AuditEvent(
                actor_did="did:arc:test:exec/aabbccdd",
                action="trace.checkpoint",
                target="traces",
                outcome="allow",
                extra={"head_hash": "b" * 64, "record_count": 2},
            ),
            sink,
        )

        anchor = read_verified_anchor(path, kp.public_key, cipher=cipher)

        assert anchor is not None
        assert anchor["head_hash"] == "b" * 64


class TestKeyDerivation:
    def test_derived_key_is_not_the_seed(self) -> None:
        """The at-rest key is domain-separated from the signing seed it comes from."""
        assert derive_record_key(_SEED) != _SEED

    def test_derivation_is_deterministic(self) -> None:
        """A restart must reach the same key from the same custodied seed."""
        assert derive_record_key(_SEED) == derive_record_key(_SEED)

    def test_a_short_seed_is_refused(self) -> None:
        """Fail closed rather than derive a key from truncated material."""
        with pytest.raises(ValueError):
            derive_record_key(b"too-short")
