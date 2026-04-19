"""Memory ACL error types.

Distinct exception hierarchy so callers can catch ACL violations
separately from general ToolErrors or IdentityErrors.

Note: Exception names use domain terminology (e.g. ACLViolation,
CapabilityExpired) rather than the *Error suffix convention because
these signal specific authorization conditions, not programming errors.
"""

from __future__ import annotations


class ACLViolation(Exception):  # noqa: N818
    """Raised when a memory operation violates the session ACL."""

    def __init__(self, reason: str, caller_did: str = "", target_did: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.caller_did = caller_did
        self.target_did = target_did


class CapabilityExpired(Exception):  # noqa: N818
    """Raised when a capability has expired (turn ended)."""

    def __init__(self, capability_id: str, turn_id: str) -> None:
        super().__init__(f"Capability {capability_id} expired (turn {turn_id})")
        self.capability_id = capability_id
        self.turn_id = turn_id


class CapabilityInvalid(Exception):  # noqa: N818
    """Raised when a capability fails signature verification or is malformed."""

    def __init__(self, capability_id: str, detail: str) -> None:
        super().__init__(f"Capability {capability_id} invalid: {detail}")
        self.capability_id = capability_id
        self.detail = detail
