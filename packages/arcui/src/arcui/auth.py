"""AuthMiddleware — bearer token authentication with viewer/operator roles.

Token → role mapping is configured at app startup. Auto-generates a viewer
token if none provided.

Security model (federal-first, zero-trust):
  - All /api/* routes require a valid bearer token.
  - Missing or invalid tokens return 401.
  - /api/health is exempt (liveness probes must not need credentials).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from collections import OrderedDict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from arcui.audit import SessionStartFields, UIAuditEvent

logger = logging.getLogger(__name__)

# SessionTracker bounds (review H-1). NAT/CGNAT clients accrete `(token,
# addr)` entries indefinitely; without bounds a long-running federal
# deployment OOMs the UI process. Defaults are conservative — 10K
# sessions and 1K bootstrap markers cap memory at < 5 MB total.
# Wave 2 TD-MED: env overrides for federal tuning without code edit.
_DEFAULT_MAX_SESSIONS = int(os.environ.get("ARCUI_MAX_SESSIONS", "10000"))
_DEFAULT_MAX_BOOTSTRAP_MARKERS = int(os.environ.get("ARCUI_MAX_BOOTSTRAP_MARKERS", "1000"))


def _resolve_username(uid: int) -> str:
    """Best-effort POSIX username for the given uid (SPEC-025 §FR-7 + §M-5).

    Returns the ``pw_name`` on POSIX hosts. On lookup failure (Windows,
    container without /etc/passwd, deleted user), returns
    ``<unknown:uid=N>`` so different uids never collapse into a single
    ``<unknown>`` audit identity — a federal scanner relies on per-user
    attribution, and silent collisions would mask the gap.
    """
    try:
        # pwd is unavailable on Windows; the ImportError branch handles
        # that (Windows is not a federal target tier so degrading to a
        # uid-only audit identity is acceptable). mypy on POSIX can find
        # the stub, so no ``type: ignore`` is needed.
        import pwd
    except ImportError:
        return f"<unknown:uid={uid}>"
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        return f"<unknown:uid={uid}>"


class _BoundedLRU:
    """Tiny LRU that evicts oldest on overflow.

    Two internal stores in `SessionTracker` had identical eviction
    logic; centralizing it here removes the duplicated `while-popitem`
    loop and gives the tracker one place to instrument (eviction
    metrics, future TTL) instead of two.
    """

    __slots__ = ("_data", "_max")

    def __init__(self, max_size: int) -> None:
        self._data: OrderedDict[Any, Any] = OrderedDict()
        self._max = max_size

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: Any) -> Any:
        return self._data.get(key)

    def touch(self, key: Any) -> None:
        """Move an existing key to most-recently-used position."""
        if key in self._data:
            self._data.move_to_end(key)

    def put(self, key: Any, value: Any) -> None:
        """Insert/update; evict oldest if size exceeds the bound."""
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


class SessionTracker:
    """LRU-bounded map from token-hash → session_id; emits at-most-once audits.

    SPEC-019 SR-3, T5.3. The tracker is consulted by AuthMiddleware on every
    successful authenticated request and answers two questions:
      1. Have we seen this (token, remote_addr) pair before? If not, emit
         `ui.session_start` and remember it.
      2. Was the token delivered via URL-hash bootstrap on loopback? If so,
         label the auth_method as `browser_bootstrap`; otherwise it's
         `manual_token`.

    Tokens are SHA-256 hashed before storage so a memory dump or audit log
    of session ids cannot be reversed to the bearer token (SR-2).

    Both internal stores are bounded LRUs (review H-1). On overflow, the
    oldest entry is evicted; that re-emits `ui.session_start` for a long-
    idle client when it returns, which is the auditable-correct behavior.
    """

    def __init__(
        self,
        *,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
        max_bootstrap_markers: int = _DEFAULT_MAX_BOOTSTRAP_MARKERS,
    ) -> None:
        self._sessions: _BoundedLRU = _BoundedLRU(max_sessions)
        self._bootstrap_token_hashes: _BoundedLRU = _BoundedLRU(max_bootstrap_markers)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def mark_bootstrap_issued(self, token: str) -> None:
        """Record that this token was delivered to the browser via URL hash.

        Called by `arc ui start` on loopback bind right before opening the
        browser. Marker survives across requests but resets on process
        restart — sessions must re-establish trust then.
        """
        self._bootstrap_token_hashes.put(self._hash(token), None)

    def observe(self, token: str, remote_addr: str) -> tuple[str, str] | None:
        """Return (session_id, auth_method) on first sighting; None on repeat.

        First sighting per (token, remote_addr) returns the new session_id
        and the auth_method label. Subsequent calls return None — the
        caller must NOT emit an audit event again for that session.
        """
        token_hash = self._hash(token)
        key = (token_hash, remote_addr)
        if key in self._sessions:
            self._sessions.touch(key)
            return None
        session_id = secrets.token_hex(8)
        self._sessions.put(key, session_id)
        if token_hash in self._bootstrap_token_hashes:
            return session_id, "browser_bootstrap"
        return session_id, "manual_token"

    def session_id_for(self, token: str, remote_addr: str) -> str:
        """Return the stable session id for a (token, addr), creating if absent.

        Unlike :meth:`observe` this never gates on first-sight — it always
        yields the id so a mutation audit (COMP-010) can attribute the change
        to the session on every request, not only the session-start one.
        """
        key = (self._hash(token), remote_addr)
        existing = self._sessions.get(key)
        if existing is not None:
            self._sessions.touch(key)
            return str(existing)
        session_id = secrets.token_hex(8)
        self._sessions.put(key, session_id)
        return session_id


class AuthConfig:
    """Token-to-role mapping. Auto-generates tokens if not provided."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.viewer_token: str = cfg.get("viewer_token") or secrets.token_hex(32)
        self.operator_token: str = cfg.get("operator_token") or secrets.token_hex(32)

    def validate_token(self, token: str) -> str | None:
        """Return role for token, or None if invalid.

        Uses constant-time comparison to prevent timing side-channel attacks.
        Roles: "operator" (read + control), "viewer" (read).
        """
        if hmac.compare_digest(token, self.operator_token):
            return "operator"
        if hmac.compare_digest(token, self.viewer_token):
            return "viewer"
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that validates bearer tokens on /api/* routes.

    Enforcement rules:
      - Non-API routes (/static, /, etc.): pass through, role=None.
      - /api/health: exempt from auth (liveness probes).
      - All other /api/* routes: require a valid bearer token.
        - No token → 401 {"error": "Missing token"}
        - Invalid token → 401 {"error": "Invalid token"}
        - Valid viewer/operator token → request.state.role set accordingly.
    """

    def __init__(self, app: Any, auth_config: AuthConfig) -> None:
        super().__init__(app)
        self._auth = auth_config

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip auth for non-API routes (static files, dashboard SPA, etc.)
        if not path.startswith("/api/"):
            request.state.role = None
            return await call_next(request)

        # /api/health is exempt — liveness probes must work without credentials.
        if path == "/api/health":
            request.state.role = None
            return await call_next(request)

        # All other /api/* routes require a valid human (viewer/operator) token.
        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()

        if not token:
            logger.warning("auth.missing_token path=%s", path)
            return JSONResponse({"error": "Missing token"}, status_code=401)

        role = self._auth.validate_token(token)

        if role is None:
            logger.warning("auth.invalid_token path=%s", path)
            return JSONResponse({"error": "Invalid token"}, status_code=401)

        request.state.role = role
        # SPEC-019 T5.3: emit session_start at-most-once per (token, addr).
        self._maybe_emit_session_start(request, token)
        # COMP-010: expose the session id so mutation routes can attribute an
        # audit event to this operator session on every request.
        self._set_session_id(request, token)
        logger.debug("auth.ok path=%s role=%s", path, role)
        return await call_next(request)

    @staticmethod
    def _set_session_id(request: Request, token: str) -> None:
        """Stash the auth-layer session id on request.state (COMP-010).

        Runs after ``_maybe_emit_session_start`` so first-sight session
        creation (and its at-most-once audit) has already happened; this just
        reads back the stable id. No tracker (a bare test app) leaves the
        attribute unset — the mutation audit helper then falls back to
        ``"unknown"``.
        """
        tracker = getattr(request.app.state, "session_tracker", None)
        if tracker is None:
            return
        client = request.client
        remote_addr = client.host if client is not None else "unknown"
        request.state.session_id = tracker.session_id_for(token, remote_addr)

    @staticmethod
    def _maybe_emit_session_start(request: Request, token: str) -> None:
        """Emit `ui.session_start` exactly once per (token, remote_addr).

        SR-3 + SPEC-025 §FR-7 mandate five fields — `session_id`, `uid`,
        `username`, `remote_addr`, `auth_method` — so federal auditors can
        attribute the session to the named OS user running the UI server
        (NIST AU-3 non-repudiation; closes FedRAMP Low gate).
        The `SessionStartFields` Pydantic model makes drop-a-field a
        type error rather than a silent audit gap.
        Looks up the SessionTracker on app.state; absent means a test
        harness without one — silently no-op so test apps stay simple.
        """
        tracker = getattr(request.app.state, "session_tracker", None)
        audit = getattr(request.app.state, "audit", None)
        if tracker is None or audit is None:
            return

        client = request.client
        remote_addr = client.host if client is not None else "unknown"
        observation = tracker.observe(token, remote_addr)
        if observation is None:
            return
        session_id, auth_method = observation
        uid = os.getuid()
        fields = SessionStartFields(
            session_id=session_id,
            uid=uid,
            username=_resolve_username(uid),
            remote_addr=remote_addr,
            auth_method=auth_method,
        )
        audit.audit_event(UIAuditEvent.SESSION_START, fields.model_dump())
