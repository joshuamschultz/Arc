"""HKDF child DID derivation helper for the delegate module.

Thin wrapper around arctrust.derive_child_identity so arcagent does not need
to reimplement HKDF logic. This module is the ONLY place in arcagent that
touches the derivation — arcagent.core.identity is NOT modified (constraint
from SPEC-018 implementation brief).

Security: parent_sk_bytes must never be logged or returned to LLM output (LLM02).
"""

from __future__ import annotations

from arctrust import ChildIdentity, derive_child_identity


def derive_delegate_child_identity(
    parent_sk_bytes: bytes,
    spawn_id: str,
    wallclock_timeout_s: int = 300,
) -> ChildIdentity:
    """Derive an ephemeral child identity for a delegation.

    Delegates directly to arcrun's HKDF implementation. arcagent owns WHAT
    to delegate; arcrun owns HOW to derive the identity.

    Args:
        parent_sk_bytes: Parent agent's Ed25519 signing key bytes.
        spawn_id: Unique nonce for this delegation (usually a UUID).
        wallclock_timeout_s: TTL for the child identity.

    Returns:
        ChildIdentity with derived DID and ephemeral signing key.
    """
    return derive_child_identity(
        parent_sk_bytes=parent_sk_bytes,
        spawn_id=spawn_id,
        wallclock_timeout_s=wallclock_timeout_s,
    )
