"""Test: pairing signature required at ALL tiers — personal tier cannot bypass.

ASI07: Mutual identity verification is the foundation of inter-agent trust.
Tier-stringency knob: at personal, self-signed keys are accepted (tier-1 trust anchor).
At enterprise/federal the signing key must chain to operator trust anchors.

RED phase: these tests fail before the personal-tier early-return is removed.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from arcgateway.pairing import (
    _ADD_SIGNED_BY_DID_COLUMN,
    _SCHEMA_SQL,
    PairingSignatureInvalid,
)
from arcgateway.pairing_signature import PairingSignatureVerifier


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    try:
        conn.execute(_ADD_SIGNED_BY_DID_COLUMN)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def _make_row(
    conn: sqlite3.Connection,
    platform: str = "telegram",
    minted_at: float = 1000.0,
    expires_at: float = 4600.0,
) -> sqlite3.Row:
    conn.execute(
        """INSERT INTO pairing_codes(code, platform, user_hash, minted_at, expires_at, consumed)
           VALUES ('TESTCODE', ?, 'hash1', ?, ?, 0)""",
        (platform, minted_at, expires_at),
    )
    conn.commit()
    return conn.execute("SELECT * FROM pairing_codes WHERE code = 'TESTCODE'").fetchone()


def _noop_failure(conn: sqlite3.Connection, platform: str, now: float) -> None:
    pass


def _noop_audit(event: str, details: dict) -> None:  # type: ignore[type-arg]
    pass


class TestPersonalTierSignatureRequired:
    """Personal tier must reject unsigned pairing requests (ASI07 pillar)."""

    def test_personal_tier_no_signature_raises(self) -> None:
        """Personal tier + no signature → PairingSignatureInvalid.

        RED: currently fails because personal tier returns early (no-op).
        After fix: personal tier requires signature (self-signed accepted).
        """
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn)

        with pytest.raises(PairingSignatureInvalid):
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code="TESTCODE",
                approver_did="did:arc:operator:alice",
                signature=None,
                now=time.time(),
                record_failure_fn=_noop_failure,
                audit_fn=_noop_audit,
            )

    def test_personal_tier_no_signature_records_failure(self) -> None:
        """Personal tier + no signature → failure is recorded (not silently dropped)."""
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn)

        failures: list[str] = []

        def capture_failure(c: sqlite3.Connection, platform: str, now: float) -> None:
            failures.append(platform)

        with pytest.raises(PairingSignatureInvalid):
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code="TESTCODE",
                approver_did="did:arc:operator:alice",
                signature=None,
                now=time.time(),
                record_failure_fn=capture_failure,
                audit_fn=_noop_audit,
            )

        # Failure must be recorded so brute-force is detected.
        assert failures, "Personal tier: missing signature must record a failure"

    def test_personal_tier_no_signature_emits_audit_event(self) -> None:
        """Personal tier + no signature → audit event emitted."""
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn)

        audit_events: list[str] = []

        def capture_audit(event: str, details: dict) -> None:  # type: ignore[type-arg]
            audit_events.append(event)

        with pytest.raises(PairingSignatureInvalid):
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code="TESTCODE",
                approver_did="did:arc:operator:alice",
                signature=None,
                now=time.time(),
                record_failure_fn=_noop_failure,
                audit_fn=capture_audit,
            )

        assert audit_events, "Personal tier: missing signature must emit an audit event"

    def test_personal_tier_self_signed_key_accepted(self) -> None:
        """Personal tier + valid self-signed key → accepted (tier-1 trust anchor).

        At personal tier, the operator's own key is the trust anchor — no
        chain to an issuer is required. The key is loaded via trust_store
        (same path as enterprise/federal) but the trust_dir points to a local
        directory where the operator stores their own pubkey.

        This test uses a real Ed25519 keypair to prove the verification path
        works end-to-end at personal tier.
        """
        from nacl.signing import SigningKey

        from arcgateway.pairing import build_pairing_challenge

        # Generate a fresh keypair for this test.
        signing_key = SigningKey.generate()
        verify_key_bytes = bytes(signing_key.verify_key)

        # Build a real challenge and signature.
        minted_at = 1000.0
        code = "TESTCODE"
        challenge = build_pairing_challenge(code, minted_at)
        signature = bytes(signing_key.sign(challenge).signature)

        # Stub trust store: return our pubkey when asked for this DID.
        import unittest.mock as mock

        approver_did = "did:arc:operator:self"

        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn, minted_at=minted_at, expires_at=minted_at + 3600)

        with mock.patch(
            "arctrust.trust_store.load_operator_pubkey",
            return_value=verify_key_bytes,
        ):
            # Must NOT raise — self-signed is accepted at personal tier.
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code=code,
                approver_did=approver_did,
                signature=signature,
                now=minted_at + 10,
                record_failure_fn=_noop_failure,
                audit_fn=_noop_audit,
            )

    def test_personal_tier_bad_signature_rejected(self) -> None:
        """Personal tier + present-but-invalid signature → PairingSignatureInvalid.

        Once personal tier stops being a no-op, a garbage signature must still
        be rejected (fail-closed on bad sig — this was already true for
        enterprise/federal).
        """
        import unittest.mock as mock

        from nacl.signing import SigningKey

        from arcgateway.pairing import build_pairing_challenge

        # Generate a keypair but sign with a *different* key than we'll verify with.
        real_key = SigningKey.generate()
        other_key = SigningKey.generate()
        minted_at = 1000.0
        challenge = build_pairing_challenge("TESTCODE", minted_at)
        # Sign with real_key but register other_key as the pubkey — mismatch.
        signature = bytes(real_key.sign(challenge).signature)
        verify_key_bytes = bytes(other_key.verify_key)

        approver_did = "did:arc:operator:self"
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn, minted_at=minted_at, expires_at=minted_at + 3600)

        with mock.patch(
            "arctrust.trust_store.load_operator_pubkey",
            return_value=verify_key_bytes,
        ):
            with pytest.raises(PairingSignatureInvalid):
                verifier.enforce_policy(
                    conn=conn,
                    row=row,
                    code="TESTCODE",
                    approver_did=approver_did,
                    signature=signature,
                    now=minted_at + 10,
                    record_failure_fn=_noop_failure,
                    audit_fn=_noop_audit,
                )
