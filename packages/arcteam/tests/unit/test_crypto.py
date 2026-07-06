"""Unit tests for arcteam.crypto — Ed25519 message signing + replay (real keys)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from arctrust import AgentIdentity, generate_keypair

from arcteam.crypto import (
    MessageSigner,
    ReplayCache,
    new_nonce,
    sign_message,
    verify_message,
)
from arcteam.types import Message


def _msg(body: str = "hello") -> Message:
    return Message(
        id="msg_1",
        ts=datetime.now(UTC).isoformat(),
        sender="agent://a1",
        to=["agent://a2"],
        body=body,
    )


class TestMessageSignerFromIdentity:
    def test_builds_signer_that_signs_verifiably(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        signer = MessageSigner.from_identity(identity)
        assert signer.did == identity.did
        msg = _msg()
        msg.signer_did = signer.did
        msg.nonce = new_nonce()
        sign_message(msg, signer.private_key)
        assert verify_message(msg, identity.public_key) is True

    def test_verify_only_identity_raises(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        verify_only = AgentIdentity(
            did=identity.did, public_key=identity.public_key, _signing_key=None
        )
        with pytest.raises(ValueError, match="no private key"):
            MessageSigner.from_identity(verify_only)


class TestSignVerify:
    def test_sign_then_verify_ok(self) -> None:
        kp = generate_keypair()
        msg = _msg()
        msg.signer_did = "did:arc:local:agent/a1"
        msg.nonce = new_nonce()
        sign_message(msg, kp.private_key)
        assert msg.sig != ""
        assert verify_message(msg, kp.public_key) is True

    def test_tampered_body_fails(self) -> None:
        kp = generate_keypair()
        msg = _msg("original")
        msg.nonce = new_nonce()
        sign_message(msg, kp.private_key)
        msg.body = "tampered"
        assert verify_message(msg, kp.public_key) is False

    def test_wrong_key_fails(self) -> None:
        kp = generate_keypair()
        other = generate_keypair()
        msg = _msg()
        msg.nonce = new_nonce()
        sign_message(msg, kp.private_key)
        assert verify_message(msg, other.public_key) is False

    def test_unsigned_message_fails_verify(self) -> None:
        kp = generate_keypair()
        assert verify_message(_msg(), kp.public_key) is False

    def test_nonce_is_unique(self) -> None:
        assert new_nonce() != new_nonce()


class TestReplayCache:
    def test_first_sight_ok_replay_rejected(self) -> None:
        cache = ReplayCache(window_seconds=300)
        nonce = new_nonce()
        ts = datetime.now(UTC).isoformat()
        assert cache.check_and_record(nonce, ts) is True
        assert cache.check_and_record(nonce, ts) is False

    def test_distinct_nonces_ok(self) -> None:
        cache = ReplayCache(window_seconds=300)
        ts = datetime.now(UTC).isoformat()
        assert cache.check_and_record(new_nonce(), ts) is True
        assert cache.check_and_record(new_nonce(), ts) is True

    def test_stale_timestamp_rejected(self) -> None:
        cache = ReplayCache(window_seconds=60)
        old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        assert cache.check_and_record(new_nonce(), old) is False

    def test_expired_nonce_pruned_allows_reuse(self) -> None:
        cache = ReplayCache(window_seconds=1)
        nonce = new_nonce()
        recent = datetime.now(UTC).isoformat()
        assert cache.check_and_record(nonce, recent) is True
        # An entry older than the window is pruned, not treated as a live replay.
        cache._seen[nonce] = datetime.now(UTC) - timedelta(seconds=5)
        assert cache.check_and_record(nonce, datetime.now(UTC).isoformat()) is True

    def test_malformed_timestamp_rejected(self) -> None:
        cache = ReplayCache(window_seconds=60)
        assert cache.check_and_record(new_nonce(), "not-a-timestamp") is False


class TestMessageSigner:
    def test_signer_carries_did_and_key(self) -> None:
        kp = generate_keypair()
        signer = MessageSigner(did="did:arc:local:agent/a1", private_key=kp.private_key)
        msg = _msg()
        msg.signer_did = signer.did
        msg.nonce = new_nonce()
        sign_message(msg, signer.private_key)
        assert verify_message(msg, kp.public_key) is True
