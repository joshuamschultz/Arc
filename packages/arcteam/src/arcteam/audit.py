"""Tamper-evident audit logger with a chained asymmetric signature per record.

Each record is signed with an arctrust :class:`~arctrust.signer.Signer` over
``prev_signature || canonical(record)`` — a per-record Ed25519 (or ECDSA-P256)
signature, NOT a symmetric HMAC. This is non-repudiable (AU-10): the verifier
holds only the operator public key, so it can prove a record's origin without
ever possessing signing material (the HMAC scheme could not — the verifier held
the same secret it would need to forge). arcteam owns *what* to sign and *when*;
the primitive lives in arctrust (SPEC-037 REQ-002, boundary).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from arctrust.signer import Signer, verify_signature

from arcteam.storage import StorageBackend
from arcteam.types import AuditRecord

logger = logging.getLogger(__name__)

AUDIT_COLLECTION = "audit"
AUDIT_STREAM_KEY = "audit"

# Batch size for chunked chain verification (limits memory per iteration)
_VERIFY_BATCH_SIZE = 1000

# Fields excluded from the signed payload: the signature triad (set after
# signing) and the transport ``seq`` mirror. ``audit_seq`` stays IN the payload
# so record reordering is detectable.
_SIGN_EXCLUDE = frozenset({"signature", "public_key", "algorithm", "key_ref", "seq"})


def _canonical_record(record_dict: dict[str, Any]) -> str:
    """Deterministic JSON over the audit-content fields (signature excluded)."""
    clean = {k: v for k, v in record_dict.items() if k not in _SIGN_EXCLUDE}
    return json.dumps(clean, sort_keys=True)


def _signing_input(record_dict: dict[str, Any], prev_signature: str) -> bytes:
    """The chained signing input: ``prev_signature || canonical(record)``."""
    return (prev_signature + _canonical_record(record_dict)).encode("utf-8")


class AuditLogger:
    """Append-only audit trail with a chained per-record signature. AU-2/AU-9/AU-10.

    Args:
        backend: Durable stream storage.
        signer: The arctrust :class:`~arctrust.signer.Signer` (in-process or
            vault-transit) that signs each record. Verification uses this
            signer's public key — a forger cannot re-sign the chain under a key
            it substitutes, because ``verify_chain`` checks against the known
            operator public key, not the key embedded in a record.
    """

    def __init__(self, backend: StorageBackend, signer: Signer) -> None:
        self._backend = backend
        self._signer = signer
        self._seq = 0
        self._prev_signature = ""

    async def _load_last(self) -> None:
        """Load the last audit seq and signature via O(1) read_last."""
        last = await self._backend.read_last(AUDIT_COLLECTION, AUDIT_STREAM_KEY)
        if last:
            self._seq = last.get("audit_seq", 0)
            self._prev_signature = last.get("signature", "")

    async def initialize(self) -> None:
        """Load state from existing audit stream. Call once after construction."""
        await self._load_last()

    async def log(
        self,
        event_type: str,
        subject: str,
        actor_id: str,
        detail: str,
        stream: str = "",
        msg_seq: int | None = None,
        target_id: str | None = None,
        classification: str = "UNCLASSIFIED",
    ) -> None:
        """Append an audit record with a chained asymmetric signature."""
        self._seq += 1
        record = AuditRecord(
            audit_seq=self._seq,
            event_type=event_type,
            stream=stream,
            msg_seq=msg_seq,
            subject=subject,
            actor_id=actor_id,
            target_id=target_id,
            classification=classification,
            timestamp_utc=datetime.now(UTC).isoformat(),
            detail=detail,
        )
        record_dict = record.model_dump()
        signature = self._signer.sign(_signing_input(record_dict, self._prev_signature)).hex()
        record_dict["signature"] = signature
        record_dict["public_key"] = self._signer.public_key.hex()
        record_dict["algorithm"] = self._signer.algorithm
        self._prev_signature = signature
        # Add seq for read_stream compatibility (mirrors audit_seq)
        record_dict["seq"] = self._seq

        await self._backend.append(AUDIT_COLLECTION, AUDIT_STREAM_KEY, record_dict)

    async def verify_chain(self) -> tuple[bool, int]:
        """Verify the signature chain in batches. Returns (valid, last_verified_seq).

        Each record's signature is checked against the KNOWN operator public key
        (this logger's signer), never the key embedded in the record — so a
        record re-signed under a substituted key fails verification.
        """
        public_key = self._signer.public_key
        algorithm = self._signer.algorithm
        prev_signature = ""
        last_verified = 0
        expected_seq = 1
        after_seq = 0

        while True:
            records = await self._backend.read_stream(
                AUDIT_COLLECTION,
                AUDIT_STREAM_KEY,
                after_seq=after_seq,
                limit=_VERIFY_BATCH_SIZE,
            )
            if not records:
                break

            for record in records:
                seq = record.get("audit_seq", 0)
                if seq != expected_seq:
                    logger.warning("Audit sequence gap: expected %d, got %d", expected_seq, seq)
                    return False, last_verified

                stored_sig = record.get("signature", "")
                try:
                    signature = bytes.fromhex(stored_sig)
                except ValueError:
                    logger.warning("Audit signature not hex at seq %d", seq)
                    return False, last_verified

                if not verify_signature(
                    algorithm, _signing_input(record, prev_signature), signature, public_key
                ):
                    logger.warning("Audit signature mismatch at seq %d", seq)
                    return False, last_verified

                prev_signature = stored_sig
                last_verified = seq
                expected_seq += 1
                after_seq = seq

            # If we got fewer records than batch size, we've read everything
            if len(records) < _VERIFY_BATCH_SIZE:
                break

        return True, last_verified
