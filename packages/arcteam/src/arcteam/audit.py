"""Tamper-evident audit logger with chained HMACs for ArcTeam messaging."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from typing import Any

from arcteam.storage import StorageBackend
from arcteam.types import AuditRecord

logger = logging.getLogger(__name__)

AUDIT_COLLECTION = "audit"
AUDIT_STREAM_KEY = "audit"

# Batch size for chunked chain verification (limits memory per iteration)
_VERIFY_BATCH_SIZE = 1000

# Fields excluded from HMAC computation (not part of audit content)
_HMAC_EXCLUDE = frozenset({"hmac_sha256", "seq"})


def _compute_record_hmac(record_dict: dict[str, Any], prev_hmac: str, hmac_key: bytes) -> str:
    """Compute chained HMAC: prev_hmac + json.dumps(record_without_hmac, sort_keys=True)."""
    clean = {k: v for k, v in record_dict.items() if k not in _HMAC_EXCLUDE}
    payload = prev_hmac + json.dumps(clean, sort_keys=True)
    return hmac.new(hmac_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class AuditLogger:
    """Append-only audit trail with chained HMACs. NIST 800-53 AU-2/AU-9."""

    def __init__(self, backend: StorageBackend, hmac_key: bytes | None = None) -> None:
        self._backend = backend
        self._seq = 0
        self._prev_hmac = ""

        if hmac_key:
            self._hmac_key = hmac_key
        else:
            # Generate a random session key instead of a hardcoded fallback.
            # Chain will only verify within the same session unless a persistent key is provided.
            self._hmac_key = secrets.token_bytes(32)
            logger.warning(
                "HMAC key not provided; using random session key. "
                "Set ARCTEAM_HMAC_KEY for persistent chain verification."
            )

    async def _load_last(self) -> None:
        """Load the last audit seq and HMAC via O(1) read_last."""
        last = await self._backend.read_last(AUDIT_COLLECTION, AUDIT_STREAM_KEY)
        if last:
            self._seq = last.get("audit_seq", 0)
            self._prev_hmac = last.get("hmac_sha256", "")

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
        """Append an audit record with chained HMAC."""
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
        record_dict["hmac_sha256"] = _compute_record_hmac(
            record_dict, self._prev_hmac, self._hmac_key
        )
        self._prev_hmac = record_dict["hmac_sha256"]
        # Add seq for read_stream compatibility (mirrors audit_seq)
        record_dict["seq"] = self._seq

        await self._backend.append(AUDIT_COLLECTION, AUDIT_STREAM_KEY, record_dict)

    async def verify_chain(self) -> tuple[bool, int]:
        """Verify HMAC chain integrity in batches. Returns (valid, last_verified_seq)."""
        prev_hmac = ""
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

                stored_hmac = record.get("hmac_sha256", "")
                expected_hmac = _compute_record_hmac(record, prev_hmac, self._hmac_key)

                if not hmac.compare_digest(stored_hmac, expected_hmac):
                    logger.warning("Audit HMAC mismatch at seq %d", seq)
                    return False, last_verified

                prev_hmac = stored_hmac
                last_verified = seq
                expected_seq += 1
                after_seq = seq

            # If we got fewer records than batch size, we've read everything
            if len(records) < _VERIFY_BATCH_SIZE:
                break

        return True, last_verified

    @staticmethod
    def load_hmac_key(env_var: str = "ARCTEAM_HMAC_KEY") -> bytes | None:
        """Load HMAC key from environment variable."""
        key = os.environ.get(env_var)
        if key:
            return key.encode("utf-8")
        return None
