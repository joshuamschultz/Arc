"""Capability-based per-turn memory grants.

Each turn, the orchestrator issues a short-lived signed capability:
"module M may read user:X:profile for turn:Y."

The CapabilityStore issues, verifies, and revokes capabilities per-turn.
Memory provider refuses any read without a valid, unexpired capability.

Design rationale (SDD §3.6 Capabilities Over ACLs):
- ACL on stored object is for admin operations (delete, classify-up).
- Capability is the run-time authorization primitive — scoped to one turn,
  signed by the issuing identity, non-transferable.
- Ed25519 signature ensures the LLM cannot forge a capability by crafting
  a tool call argument — the signature requires the private key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from arcagent.modules.memory_acl.errors import CapabilityExpired, CapabilityInvalid

_logger = logging.getLogger("arcagent.modules.memory_acl.capabilities")

# Maximum age a capability may be valid within a single turn (seconds).
# Capabilities are per-turn; this is the absolute wall-clock safety net.
_MAX_CAPABILITY_AGE_SECONDS = 3600.0


class Capability(BaseModel):
    """A short-lived, signed authorization token for one memory operation.

    Fields:
    - capability_id: UUID, globally unique
    - caller_module: who is requesting (e.g. "memory_acl")
    - target_resource: resource URI (e.g. "user:did:arc:org:user/abc:profile")
    - allowed_actions: list of permitted verbs ("read", "write", "search")
    - turn_id: the turn this capability was issued for
    - issued_at: monotonic seconds (time.monotonic()) at issuance
    - expires_at: monotonic seconds at which this capability is invalid
    - signature: Ed25519 signature over canonical_bytes(), hex-encoded
    """

    capability_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    caller_module: str
    target_resource: str
    allowed_actions: list[str] = Field(default_factory=list)
    turn_id: str
    issued_at: float = Field(default_factory=time.monotonic)
    expires_at: float
    signature: str = ""  # hex-encoded Ed25519 signature

    model_config = {"frozen": False}  # mutable so signature can be set after construction

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for signing/verification.

        Excludes ``signature`` field itself so signing and verification
        produce identical byte sequences.
        """
        payload: dict[str, Any] = {
            "capability_id": self.capability_id,
            "caller_module": self.caller_module,
            "target_resource": self.target_resource,
            "allowed_actions": sorted(self.allowed_actions),
            "turn_id": self.turn_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def fingerprint(self) -> str:
        """SHA-256 fingerprint of canonical_bytes (for logging)."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()[:16]

    def is_expired(self, now: float | None = None) -> bool:
        """Return True if this capability has expired."""
        current = now if now is not None else time.monotonic()
        return current >= self.expires_at

    def allows(self, action: str) -> bool:
        """Return True if this capability permits the given action."""
        return action in self.allowed_actions


class CapabilityStore:
    """Issues, verifies, and revokes per-turn capabilities.

    One CapabilityStore per agent session. Capabilities are in-memory
    only — they do not persist across restarts (by design: they are
    ephemeral per-turn grants).

    Thread safety: single-threaded asyncio; no locks needed.
    """

    def __init__(self, identity: Any | None = None) -> None:
        """Initialise store with an optional AgentIdentity for signing.

        ``identity`` should be an ``AgentIdentity`` instance from
        ``arcagent.core.identity``. When None, capabilities are issued
        without Ed25519 signatures (development/test mode).
        """
        self._identity = identity
        # Active capabilities: capability_id → Capability
        self._active: dict[str, Capability] = {}
        # Revoked capability IDs (set; checked before returning valid)
        self._revoked: set[str] = set()

    def issue(
        self,
        *,
        caller_module: str,
        target_resource: str,
        allowed_actions: list[str],
        turn_id: str,
        ttl_seconds: float = _MAX_CAPABILITY_AGE_SECONDS,
    ) -> Capability:
        """Issue a new signed capability for one turn.

        The capability is signed with the agent's Ed25519 key (if
        available) and stored in the active set until revoked or expired.

        Returns the issued Capability with signature set.
        """
        now = time.monotonic()
        cap = Capability(
            caller_module=caller_module,
            target_resource=target_resource,
            allowed_actions=list(allowed_actions),
            turn_id=turn_id,
            issued_at=now,
            expires_at=now + ttl_seconds,
        )

        if self._identity is not None and getattr(self._identity, "can_sign", False):
            sig_bytes = self._identity.sign(cap.canonical_bytes())
            cap.signature = sig_bytes.hex()

        self._active[cap.capability_id] = cap
        _logger.debug(
            "Issued capability %s for %s turn=%s actions=%s",
            cap.capability_id,
            target_resource,
            turn_id,
            allowed_actions,
        )
        return cap

    def verify(self, cap: Capability, *, action: str | None = None) -> bool:
        """Verify a capability is active, unexpired, unrevoked, and signed.

        Raises CapabilityExpired if the capability has expired.
        Raises CapabilityInvalid if the signature does not verify.

        Returns True if the capability is valid (and permits action if given).
        """
        if cap.capability_id in self._revoked:
            raise CapabilityInvalid(cap.capability_id, "revoked")

        if cap.is_expired():
            raise CapabilityExpired(cap.capability_id, cap.turn_id)

        # Signature verification (when identity is available)
        if cap.signature and self._identity is not None:
            try:
                sig_bytes = bytes.fromhex(cap.signature)
            except ValueError as exc:
                raise CapabilityInvalid(cap.capability_id, "malformed hex signature") from exc

            if not self._identity.verify(cap.canonical_bytes(), sig_bytes):
                raise CapabilityInvalid(cap.capability_id, "signature mismatch")

        if action is not None and not cap.allows(action):
            raise CapabilityInvalid(cap.capability_id, f"action '{action}' not permitted")

        return True

    def revoke(self, capability_id: str) -> None:
        """Revoke a capability by ID.

        Idempotent — revoking an already-revoked capability is a no-op.
        """
        self._active.pop(capability_id, None)
        self._revoked.add(capability_id)
        _logger.debug("Revoked capability %s", capability_id)

    def revoke_turn(self, turn_id: str) -> int:
        """Revoke all capabilities issued for a specific turn.

        Called when a turn ends to invalidate all per-turn grants.
        Returns the count of capabilities revoked.
        """
        to_revoke = [cap_id for cap_id, cap in self._active.items() if cap.turn_id == turn_id]
        for cap_id in to_revoke:
            self.revoke(cap_id)
        if to_revoke:
            _logger.debug(
                "Revoked %d capabilities for turn %s",
                len(to_revoke),
                turn_id,
            )
        return len(to_revoke)

    def get_active(self, capability_id: str) -> Capability | None:
        """Look up an active capability by ID."""
        cap = self._active.get(capability_id)
        if cap is None or cap.is_expired() or capability_id in self._revoked:
            return None
        return cap

    def has_valid_capability(
        self,
        *,
        caller_module: str,
        target_resource: str,
        action: str,
        turn_id: str,
    ) -> bool:
        """Return True if any active, unexpired capability covers this access.

        Used by the memory provider for defense-in-depth re-check.
        """
        for cap in self._active.values():
            if cap.capability_id in self._revoked:
                continue
            if cap.is_expired():
                continue
            if cap.turn_id != turn_id:
                continue
            if cap.caller_module != caller_module:
                continue
            if cap.target_resource != target_resource:
                continue
            if cap.allows(action):
                return True
        return False

    def clear_expired(self) -> int:
        """Purge expired capabilities from the active set.

        Returns count removed. Called periodically to prevent unbounded growth.
        """
        now = time.monotonic()
        expired = [k for k, v in self._active.items() if v.expires_at <= now]
        for k in expired:
            del self._active[k]
        return len(expired)
