"""Operator-signed loop checkpoints — integrity for crash-resume (SPEC-043 F3).

A checkpoint restores ``tokens_used`` / ``cost_usd`` on resume, so an agent that
could rewrite the persisted record could zero those counters and reset the LLM10
budget breaker on resume. The record is therefore signed with the deployment
**operator** :class:`~arctrust.signer.Signer` (SPEC-053/037 authority — the same
key that anchors WORM; the agent has no path to mint it, ASI09) and verified on
resume: a tampered, unsigned, or wrong-key checkpoint fails closed.

Lives outside ``arcagent/core`` (LOC budget) — pure wiring over arctrust.
"""

from __future__ import annotations

from typing import Any

from arctrust import canonical_json
from arctrust.signer import Signer, verify_signature

# Envelope keys wrapped around the signed scalar metadata by the persistence
# layer. They are EXCLUDED from the signed bytes so the signature is stable
# regardless of when/how the JSONL line is written (the timestamp is assigned at
# write time; ``signature`` is the field we are computing).
_ENVELOPE = frozenset({"type", "timestamp", "signature"})


def _canonical_bytes(record: dict[str, Any]) -> bytes:
    """Deterministic bytes over the signed scalar metadata (envelope excluded)."""
    payload = {k: v for k, v in record.items() if k not in _ENVELOPE}
    return canonical_json(payload)


def sign_record(record: dict[str, Any], signer: Signer) -> str:
    """Return the operator signature (hex) over the record's canonical bytes."""
    return signer.sign(_canonical_bytes(record)).hex()


def verify_record(record: dict[str, Any], *, public_key: bytes, algorithm: str) -> None:
    """Verify the checkpoint signature; raise ``ValueError`` fail-closed on failure.

    A missing signature (unsigned record) or a signature that does not verify
    against the operator public key is refused — an agent cannot reset its own
    budget counters by editing the persisted checkpoint (LLM10 / ASI06).
    """
    sig = record.get("signature")
    if not sig:
        raise ValueError("checkpoint resume refused: unsigned checkpoint (fail-closed)")
    try:
        raw = bytes.fromhex(sig)
    except (TypeError, ValueError) as exc:
        raise ValueError("checkpoint resume refused: malformed signature") from exc
    if not verify_signature(algorithm, _canonical_bytes(record), raw, public_key):
        raise ValueError("checkpoint resume refused: signature verification failed (tampered)")


__all__ = ["sign_record", "verify_record"]
