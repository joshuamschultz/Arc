"""PairingSignatureVerifier — Ed25519 signature verification for DM pairing.

Extracted from PairingStore._verify_signature_conn so that signature
verification logic has a single, testable home.

Design (SDD §3.1 T1.8.3):
    Federal tier:    signature REQUIRED; missing or bad → PairingSignatureInvalid.
    Enterprise tier: signature OPTIONAL; missing → warn audit; bad → raise.
    Personal tier:   signature IGNORED entirely (no-op).

The verifier uses PyNaCl via a lazy import so this module stays importable
in minimal test environments that lack the native extensions.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

_logger = logging.getLogger("arcgateway.pairing_signature")


class PairingSignatureVerifier:
    """Verifies Ed25519 signatures on pairing approvals.

    Attributes:
        _tier:      Deployment tier ("personal" | "enterprise" | "federal").
        _trust_dir: Override for the operator trust store directory.
    """

    def __init__(
        self,
        tier: str = "personal",
        *,
        trust_dir: Any | None = None,
    ) -> None:
        """Initialise PairingSignatureVerifier.

        Args:
            tier:      Tier string: "personal", "enterprise", or "federal".
            trust_dir: Optional Path override for the arctrust directory.
        """
        self._tier = tier
        self._trust_dir = trust_dir

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def enforce_policy(
        self,
        *,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        code: str,
        approver_did: str | None,
        signature: bytes | None,
        now: float,
        record_failure_fn: Any,
        audit_fn: Any,
    ) -> None:
        """Apply tier-driven signature policy and verify when signature present.

        Delegates to ``_handle_missing_signature`` or ``_verify`` as appropriate.

        Federal:    signature required; missing or bad → PairingSignatureInvalid.
        Enterprise: signature optional; missing → warn; bad → raise.
        Personal:   no-op.

        Args:
            conn:              Open DB connection (for failure recording).
            row:               DB row for the pairing code being approved.
            code:              8-char pairing code (for challenge construction).
            approver_did:      DID of the approving operator.
            signature:         Ed25519 signature bytes (may be None).
            now:               Current unix timestamp.
            record_failure_fn: Callable(conn, platform, now) to record failures.
            audit_fn:          Callable(event_type, details) for audit emission.

        Raises:
            PairingSignatureInvalid: On federal-tier missing sig or bad sig at
                                     any tier when signature is present.
        """
        if self._tier == "personal":
            return

        platform = row["platform"]
        minted_at = row["minted_at"]

        if signature is None:
            self._handle_missing_signature(
                conn=conn,
                code=code,
                approver_did=approver_did,
                platform=platform,
                now=now,
                record_failure_fn=record_failure_fn,
                audit_fn=audit_fn,
            )
            return

        # Signature supplied — verify regardless of tier (fail-closed on bad sig).
        self._verify(
            conn=conn,
            code=code,
            minted_at=minted_at,
            approver_did=approver_did,
            signature=signature,
            platform=platform,
            now=now,
            record_failure_fn=record_failure_fn,
            audit_fn=audit_fn,
        )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _handle_missing_signature(
        self,
        *,
        conn: sqlite3.Connection,
        code: str,
        approver_did: str | None,
        platform: str,
        now: float,
        record_failure_fn: Any,
        audit_fn: Any,
    ) -> None:
        """Handle the missing-signature case per tier policy.

        Federal: record failure + raise PairingSignatureInvalid.
        Enterprise: emit warn audit + proceed.

        Args:
            conn:              Open DB connection.
            code:              Pairing code (for code_id in audit).
            approver_did:      DID of approver.
            platform:          Platform name.
            now:               Current unix timestamp.
            record_failure_fn: Callable(conn, platform, now) for failure recording.
            audit_fn:          Callable(event_type, details) for audit emission.

        Raises:
            PairingSignatureInvalid: Federal tier only.
        """
        from arcgateway.pairing import PairingSignatureInvalid, _code_id

        if self._tier == "federal":
            record_failure_fn(conn, platform, now)
            conn.commit()
            audit_fn(
                "gateway.pairing.signature_invalid",
                {
                    "code_id": _code_id(code),
                    "approver_did": approver_did,
                    "platform": platform,
                    "reason": "missing_signature",
                },
            )
            raise PairingSignatureInvalid(
                f"Federal tier requires an Ed25519 signature on approval; "
                f"approver_did={approver_did!r} supplied none."
            )

        # Enterprise tier — warn and proceed.
        audit_fn(
            "gateway.pairing.signature_missing",
            {
                "code_id": _code_id(code),
                "approver_did": approver_did,
                "platform": platform,
                "tier": self._tier,
            },
        )

    def _verify(
        self,
        *,
        conn: sqlite3.Connection,
        code: str,
        minted_at: float,
        approver_did: str | None,
        signature: bytes,
        platform: str,
        now: float,
        record_failure_fn: Any,
        audit_fn: Any,
    ) -> None:
        """Resolve operator pubkey and verify Ed25519 signature.

        Args:
            conn:              Open DB connection (for failure recording).
            code:              Pairing code.
            minted_at:         Unix timestamp when code was minted.
            approver_did:      DID of the approving operator.
            signature:         Ed25519 signature bytes.
            platform:          Platform name.
            now:               Current unix timestamp.
            record_failure_fn: Callable(conn, platform, now).
            audit_fn:          Callable(event_type, details).

        Raises:
            PairingSignatureInvalid: If approver_did is None, trust lookup fails,
                                     or signature does not verify.
        """
        from arcgateway.pairing import (
            PairingSignatureInvalid,
            _code_id,
            build_pairing_challenge,
        )

        if approver_did is None:
            record_failure_fn(conn, platform, now)
            conn.commit()
            audit_fn(
                "gateway.pairing.signature_invalid",
                {
                    "code_id": _code_id(code),
                    "approver_did": None,
                    "platform": platform,
                    "reason": "approver_did_required_with_signature",
                },
            )
            raise PairingSignatureInvalid(
                "Signature supplied without approver_did — cannot resolve pubkey."
            )

        try:
            from arctrust.trust_store import TrustStoreError, load_operator_pubkey
            from nacl.exceptions import BadSignatureError
            from nacl.signing import VerifyKey
        except ImportError as exc:  # pragma: no cover — arctrust is a required dep
            raise PairingSignatureInvalid(
                "PyNaCl / arctrust trust store not available; cannot "
                f"verify signatures: {exc}"
            ) from exc

        try:
            pubkey = load_operator_pubkey(approver_did, trust_dir=self._trust_dir)
        except TrustStoreError as exc:
            record_failure_fn(conn, platform, now)
            conn.commit()
            audit_fn(
                "gateway.pairing.signature_invalid",
                {
                    "code_id": _code_id(code),
                    "approver_did": approver_did,
                    "platform": platform,
                    "reason": f"trust_store:{exc.code}",
                },
            )
            raise PairingSignatureInvalid(
                f"Cannot resolve operator pubkey for {approver_did!r}: "
                f"[{exc.code}] {exc.message}"
            ) from exc

        challenge = build_pairing_challenge(code, minted_at)
        try:
            VerifyKey(pubkey).verify(challenge, signature)
        except BadSignatureError as exc:
            record_failure_fn(conn, platform, now)
            conn.commit()
            audit_fn(
                "gateway.pairing.signature_invalid",
                {
                    "code_id": _code_id(code),
                    "approver_did": approver_did,
                    "platform": platform,
                    "reason": "bad_signature",
                },
            )
            raise PairingSignatureInvalid(
                f"Ed25519 signature for approver_did={approver_did!r} did not verify."
            ) from exc

        audit_fn(
            "gateway.pairing.signature_verified",
            {
                "code_id": _code_id(code),
                "signed_by_did": approver_did,
                "platform": platform,
            },
        )
