"""Unit tests for PairingSignatureVerifier — Ed25519 signature policy."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arcgateway.pairing import (
    PairingSignatureInvalid,
    _SCHEMA_SQL,
    _ADD_SIGNED_BY_DID_COLUMN,
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
    return conn.execute(
        "SELECT * FROM pairing_codes WHERE code = 'TESTCODE'"
    ).fetchone()


def _noop_failure(conn: sqlite3.Connection, platform: str, now: float) -> None:
    pass


def _noop_audit(event: str, details: dict) -> None:
    pass


# ---------------------------------------------------------------------------
# Personal tier — no-op
# ---------------------------------------------------------------------------


class TestPersonalTier:
    def test_personal_tier_ignores_missing_signature(self) -> None:
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn)
        # Should not raise regardless of signature
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

    def test_personal_tier_does_not_verify_present_signature(self) -> None:
        """Personal tier is truly a no-op — even a garbage signature passes."""
        verifier = PairingSignatureVerifier(tier="personal")
        conn = _make_db()
        row = _make_row(conn)
        # No-op: personal tier must not attempt verification
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
# Enterprise tier — missing sig warns, bad sig raises
# ---------------------------------------------------------------------------


class TestEnterpriseTier:
    def test_enterprise_missing_signature_emits_warn_audit(self) -> None:
        verifier = PairingSignatureVerifier(tier="enterprise")
        conn = _make_db()
        row = _make_row(conn)
        audit_calls: list[str] = []

        def capture_audit(event: str, details: dict) -> None:
            audit_calls.append(event)

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
        assert "gateway.pairing.signature_missing" in audit_calls

    def test_enterprise_missing_signature_does_not_raise(self) -> None:
        verifier = PairingSignatureVerifier(tier="enterprise")
        conn = _make_db()
        row = _make_row(conn)
        # Should not raise — enterprise tier only warns on missing sig
        verifier.enforce_policy(
            conn=conn,
            row=row,
            code="TESTCODE",
            approver_did=None,
            signature=None,
            now=time.time(),
            record_failure_fn=_noop_failure,
            audit_fn=_noop_audit,
        )


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
