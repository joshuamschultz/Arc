"""User profile error types.

Distinct exception hierarchy so callers can catch profile-specific
errors separately from general memory or identity errors.

N818 noqa: Exception names are part of the SDD §3.6 specification contract
and cannot be renamed to add an 'Error' suffix without a spec change.
"""

from __future__ import annotations


class ProfileNotFound(Exception):  # noqa: N818
    """Raised when no profile file exists for the given user DID."""

    def __init__(self, user_did: str) -> None:
        super().__init__(f"No profile found for user: {user_did}")
        self.user_did = user_did


class ACLViolation(Exception):  # noqa: N818
    """Raised when a profile operation violates the stored ACL."""

    def __init__(self, reason: str, user_did: str = "", caller_did: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_did = user_did
        self.caller_did = caller_did


class BodyOverflow(Exception):  # noqa: N818
    """Raised when the profile body would exceed the 2 KB hard cap.

    Callers MUST handle this by spilling overflow content to the
    episodic store rather than silently truncating.  The exception
    carries the new body size and the configured cap so callers can
    compute how much content needs to be spilled.
    """

    def __init__(self, body_size: int, cap_bytes: int) -> None:
        super().__init__(
            f"Profile body size {body_size}B exceeds {cap_bytes}B cap"
        )
        self.body_size = body_size
        self.cap_bytes = cap_bytes
