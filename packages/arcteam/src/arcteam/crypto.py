"""Ed25519 message signing and replay protection (REQ-030, REQ-031).

Signing and verification delegate to :mod:`arctrust` — arcteam owns *what* to
sign (the canonical message envelope) and *when* (sign on send, verify on
consume), never the primitives. A signature binds the semantically meaningful
envelope fields so any tampering (e.g. a rewritten ``body``) fails
verification. A ``nonce`` + ``ts`` replay window rejects re-submitted captures.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from arctrust import sign, verify

from arcteam.types import Message

# Envelope fields the signature binds. Excludes routing/transport fields that
# the backend assigns after signing (``seq``), the signature itself (``sig``),
# and mutable delivery bookkeeping (``status``, ``meta``).
_SIGNED_FIELDS = (
    "id",
    "ts",
    "nonce",
    "signer_did",
    "sender",
    "to",
    "thread_id",
    "msg_type",
    "priority",
    "action_required",
    "body",
    "mentions",
    "refs",
)


@dataclass(frozen=True)
class MessageSigner:
    """A signing identity: the sender's DID and its Ed25519 private key seed."""

    did: str
    private_key: bytes


def new_nonce() -> str:
    """Return a fresh, unguessable nonce for replay protection."""
    return secrets.token_hex(16)


def canonical_bytes(message: Message) -> bytes:
    """Deterministically serialize the signed envelope fields."""
    payload = {field: getattr(message, field) for field in _SIGNED_FIELDS}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def sign_message(message: Message, private_key: bytes) -> None:
    """Sign ``message`` in place, populating its ``sig`` (hex) field.

    Callers set ``signer_did`` and ``nonce`` before signing; both are covered
    by the signature.
    """
    message.sig = sign(canonical_bytes(message), private_key).hex()


def verify_message(message: Message, public_key: bytes) -> bool:
    """Return True iff ``message.sig`` is a valid signature by ``public_key``."""
    if not message.sig:
        return False
    try:
        signature = bytes.fromhex(message.sig)
    except ValueError:
        return False
    return verify(canonical_bytes(message), signature, public_key)


class ReplayCache:
    """Sliding-window nonce cache. Rejects replays and stale timestamps."""

    def __init__(self, window_seconds: float = 300.0) -> None:
        self._window = window_seconds
        self._seen: dict[str, datetime] = {}

    def _prune(self, now: datetime) -> None:
        cutoff = now.timestamp() - self._window
        expired = [n for n, t in self._seen.items() if t.timestamp() < cutoff]
        for nonce in expired:
            del self._seen[nonce]

    def check_and_record(self, nonce: str, ts: str) -> bool:
        """Return True if first-seen and fresh; False for replay or stale ``ts``."""
        now = datetime.now(UTC)
        self._prune(now)
        try:
            sent_at = datetime.fromisoformat(ts)
        except ValueError:
            return False
        if now.timestamp() - sent_at.timestamp() > self._window:
            return False
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True
