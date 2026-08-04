"""Browser sessions for signed-in users (SPEC-057 REQ-043).

Process-memory only, matching the package rule that arcui keeps no on-disk token
file: a restart signs everyone out, which is the correct trade for a dashboard
whose sessions are cheap to re-establish and expensive to leak.

A session carries the user's DID as well as their role, so a mutation route can
attribute what it does to a person rather than to "whoever held the token".
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# Long enough for a working day, short enough that a borrowed laptop is not a
# standing grant. Refreshed on use, so an active session does not expire mid-task.
DEFAULT_TTL_SECONDS = 12 * 60 * 60

# Failed logins are throttled per email. Argon2id already makes guessing slow;
# this stops a determined attacker from parallelising around that.
_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 300


@dataclass(frozen=True)
class Session:
    token: str
    email: str
    did: str
    role: str
    expires_at: float


class SessionRegistry:
    """Issue, validate, and revoke browser sessions. Thread-safe."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    # --- sessions -------------------------------------------------------

    def issue(self, *, email: str, did: str, role: str) -> Session:
        session = Session(
            token=secrets.token_urlsafe(32),
            email=email,
            did=did,
            role=role,
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._sessions[session.token] = session
            self._evict_expired()
        return session

    def validate(self, token: str) -> Session | None:
        """Return the live session for ``token``, refreshing its expiry."""
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at <= time.time():
                del self._sessions[token]
                return None
            # Refresh on use: expiring someone mid-task teaches them to pick a
            # longer TTL, which is the opposite of what this is for.
            refreshed = Session(
                token=session.token,
                email=session.email,
                did=session.did,
                role=session.role,
                expires_at=time.time() + self._ttl,
            )
            self._sessions[token] = refreshed
            return refreshed

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_user(self, email: str) -> int:
        """Sign a user out everywhere. Used when their password changes."""
        with self._lock:
            gone = [t for t, s in self._sessions.items() if s.email == email]
            for token in gone:
                del self._sessions[token]
        return len(gone)

    def _evict_expired(self) -> None:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[token]

    # --- throttle -------------------------------------------------------

    def locked_out(self, email: str) -> bool:
        with self._lock:
            count, until = self._failures.get(email.lower(), (0, 0.0))
            if until and until <= time.time():
                del self._failures[email.lower()]
                return False
            return count >= _MAX_FAILURES

    def record_failure(self, email: str) -> None:
        key = email.lower()
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            self._failures[key] = (count + 1, time.time() + _LOCKOUT_SECONDS)

    def clear_failures(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email.lower(), None)


__all__ = ["DEFAULT_TTL_SECONDS", "Session", "SessionRegistry"]
