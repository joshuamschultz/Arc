"""VoicePairing — a trusted mic (SPEC-077 COMP-013, REQ-017/018).

A connection is refused without a valid pairing token (no pairing → no agent
response). v1 personal tier: one operator-signed token, supplied via an env var
(a credential — never through config or the LLM, LLM07), maps to the operator
identity and a stable chat_id. Enterprise TOFU (approve-once via the SPEC-035
mechanical-approval grant store) is the follow-up; the ``authenticate`` seam is
where it plugs in.

A paired mic is a trusted *source*; its spoken content is still untrusted input
(the confirmation gate and policy pipeline guard consequential actions, REQ-018).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceIdentity:
    user_did: str
    chat_id: str


class VoicePairing:
    """Authorize a mic by a constant-time token check."""

    def __init__(self, *, token: str | None, operator_did: str, chat_id: str) -> None:
        self._token = token or ""
        self._identity = VoiceIdentity(user_did=operator_did, chat_id=chat_id)

    def is_configured(self) -> bool:
        return bool(self._token)

    def authenticate(self, token: str) -> tuple[str, str] | None:
        """Return (user_did, chat_id) for a valid token, else None (fail-closed)."""
        if not self._token or not token:
            return None
        if not hmac.compare_digest(token, self._token):
            return None
        return (self._identity.user_did, self._identity.chat_id)


__all__ = ["VoiceIdentity", "VoicePairing"]
