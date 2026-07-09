"""Memory ACL error types.

Distinct exception hierarchy so callers can catch ACL violations
separately from general ToolErrors or IdentityErrors.

Note: ``ACLViolation`` uses domain terminology rather than the ``*Error``
suffix convention because it signals a specific authorization condition,
not a programming error.
"""

from __future__ import annotations


class ACLViolation(Exception):  # noqa: N818
    """Raised when a memory operation violates the session ACL."""

    def __init__(self, reason: str, caller_did: str = "", target_did: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.caller_did = caller_did
        self.target_did = target_did
