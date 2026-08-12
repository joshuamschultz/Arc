"""Session identity — the one derivation of a conversation key (SPEC-065 COMP-007).

A session key is an *identity*: the stable name of the conversation between one
agent and one correspondent, whether that correspondent is a human on a chat
surface or a teammate agent on the message bus. Two derivations mean the same
pair gets two conversations, and a key composed outside the owner also skips
the rotation
generation, so ``/new`` silently fails to take effect on that surface.

It lives in arctrust — the leaf every layer already depends on — because every
layer needs it and none may import a layer above it: arcgateway stamps it for a
human surface, arcagent's teammate inbox stamps it for a peer, and arcui/arctui
read it back. arcagent cannot import arcgateway (the dependency arrows point one
way), so an owner inside the gateway would have forced the agent side to compose
its own key. That is the defect this module exists to make impossible.

Rotation is deliberately NOT owned here: ``generation`` is an argument, and only
the surface that owns the explicit new-session command (``SessionRouter`` and its
``SessionEpochStore``) may pass a non-zero one.
"""

from __future__ import annotations

import hashlib

__all__ = ["build_session_key"]


def build_session_key(agent_did: str, user_did: str, *, generation: int = 0) -> str:
    """Build a deterministic 16-hex-char session key from (agent, user) pair.

    Same (agent, user) pair always produces the same key, regardless of which
    platform the user messaged from — enabling cross-platform session continuity
    (D-06 and SDD §3.3).

    The key is a truncated SHA-256 digest. Truncation to 16 chars is intentional:
    collision probability is negligible for expected concurrency levels (~2^64
    preimage resistance) while keeping session keys human-readable in logs.

    ``generation`` folds a per-(agent, user) rotation counter into the key so a
    ``/new`` command can mint a fresh, empty session (see SessionEpochStore).
    ``generation=0`` reproduces the original key exactly — the correct default
    that keeps every existing on-disk session valid.

    Args:
        agent_did: The target agent's DID (e.g. "did:arc:org:agent/id").
        user_did: The DID of whoever the agent is talking to — a resolved
            cross-platform user DID, or a teammate agent's signer DID.
        generation: Session rotation counter; 0 is the first/plain session.

    Returns:
        16-character lowercase hex string.
    """
    base = f"{agent_did}:{user_did}"
    seed = base if generation == 0 else f"{base}:g{generation}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]
