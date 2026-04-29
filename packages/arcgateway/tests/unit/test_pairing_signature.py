"""Unit tests for PairingSignatureVerifier — Ed25519 signature policy.

Policy (four-pillar mandate): signature required at all tiers.
Tier sets trust-anchor stringency, not whether to check.
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
    """Create a temp SQLite DB for testing."""
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
    """Insert and return a fake pairing code row."""
    conn.execute(
        """INSERT INTO pairing_codes(code, platform, user_hash, minted_at, expires_at, consumed)
           VALUES ('TESTCODE', ?, 'hash1', ?, ?, 0)""",
        (platform, minted_at, expires_at),
    )
    conn.commit()
    return conn.execute("SELECT * FROM pairing_codes WHERE code = 'TESTCODE'").fetchone()


def _noop_failure(conn: sqlite3.Connection, platform: str, now: float) -> None:
    pass


def _noop_audit(event: str, details: dict) -> None:
    pass


# ---------------------------------------------------------------------------
# Personal tier — signature required (self-signed key accepted as trust anchor)
# ---------------------------------------------------------------------------


class TestPersonalTier:
    def test_personal_tier_missing_signature_raises(self) -> None:
        """Personal tier + missing signature → PairingSignatureInvalid (pillar: all tiers)."""
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

    def test_personal_tier_bad_signature_raises(self) -> None:
        """Personal tier: present-but-invalid signature → PairingSignatureInvalid (fail-closed)."""
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn)
        # b"garbage" will fail nacl verification → PairingSignatureInvalid
        with pytest.raises(PairingSignatureInvalid):
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code="TESTCODE",
                approver_did="did:arc:operator:alice",
                signature=b"garbage",
                now=time.time(),
                record_failure_fn=_noop_failure,
                audit_fn=_noop_audit,
            )


# ---------------------------------------------------------------------------
# Enterprise tier — signature required; all missing-sig cases raise
# ---------------------------------------------------------------------------


class TestEnterpriseTier:
    def test_enterprise_missing_signature_raises(self) -> None:
        """Enterprise tier: missing signature → PairingSignatureInvalid (pillar: all tiers)."""
        verifier = PairingSignatureVerifier(tier="enterprise")
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

    def test_enterprise_missing_signature_emits_audit_event(self) -> None:
        """Enterprise tier: missing signature emits signature_invalid audit event."""
        verifier = PairingSignatureVerifier(tier="enterprise")
        conn = _make_db()
        row = _make_row(conn)
        audit_calls: list[str] = []

        def capture_audit(event: str, details: dict) -> None:  # type: ignore[type-arg]
            audit_calls.append(event)

        with pytest.raises(PairingSignatureInvalid):
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code="TESTCODE",
                approver_did=None,
                signature=None,
                now=time.time(),
                record_failure_fn=_noop_failure,
                audit_fn=capture_audit,
            )
        assert "gateway.pairing.signature_invalid" in audit_calls


# ---------------------------------------------------------------------------
# Federal tier — missing sig raises
# ---------------------------------------------------------------------------


class TestFederalTier:
    def test_federal_missing_signature_raises(self) -> None:
        verifier = PairingSignatureVerifier(tier="federal")
        conn = _make_db()
        row = _make_row(conn)

        failures_recorded: list[str] = []

        def capture_failure(c: sqlite3.Connection, platform: str, now: float) -> None:
            failures_recorded.append(platform)

        with pytest.raises(PairingSignatureInvalid, match="Federal tier"):
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
        assert "telegram" in failures_recorded

    def test_federal_missing_approver_did_with_signature_raises(self) -> None:
        """Signature present but no approver_did → PairingSignatureInvalid."""
        verifier = PairingSignatureVerifier(tier="federal")
        conn = _make_db()
        row = _make_row(conn)

        with pytest.raises(PairingSignatureInvalid, match="approver_did"):
            verifier.enforce_policy(
                conn=conn,
                row=row,
                code="TESTCODE",
                approver_did=None,
                signature=b"some_sig",
                now=time.time(),
                record_failure_fn=_noop_failure,
                audit_fn=_noop_audit,
            )
