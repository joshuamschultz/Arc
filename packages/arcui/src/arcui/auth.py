"""AuthMiddleware — bearer token authentication with viewer/operator roles.

Token → role mapping is configured at app startup. Auto-generates a viewer
token if none provided.

Security model (federal-first, zero-trust):
  - All /api/* routes require a valid bearer token.
  - Missing or invalid tokens return 401.
  - Agent tokens are rejected on non-agent REST paths with 403 (ASI03).
  - /api/health is exempt (liveness probes must not need credentials).
  - /api/agent/* paths are exempt from HTTP-layer auth — the WebSocket
    endpoint handles first-message auth itself via authenticate_ws().
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths exempt from bearer token enforcement.
# /api/agent/* is handled by the WS endpoint's first-message auth.
_EXEMPT_PATHS = {"/api/health"}
_AGENT_PATH_PREFIX = "/api/agent/"


class AuthConfig:
    """Token-to-role mapping. Auto-generates tokens if not provided."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.viewer_token: str = cfg.get("viewer_token") or secrets.token_hex(32)
        self.operator_token: str = cfg.get("operator_token") or secrets.token_hex(32)
        self.agent_token: str = cfg.get("agent_token") or secrets.token_hex(32)

    def validate_token(self, token: str) -> str | None:
        """Return role for token, or None if invalid.

        Uses constant-time comparison to prevent timing side-channel attacks.
        Roles: "operator" (read + control), "viewer" (read), "agent" (connect + stream).
        """
        if hmac.compare_digest(token, self.operator_token):
            return "operator"
        if hmac.compare_digest(token, self.viewer_token):
            return "viewer"
        if hmac.compare_digest(token, self.agent_token):
            return "agent"
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that validates bearer tokens on /api/* routes.

    Enforcement rules:
      - Non-API routes (/static, /, etc.): pass through, role=None.
      - /api/health: exempt from auth (liveness probes).
      - /api/agent/*: pass through — WebSocket endpoint does first-message auth.
      - All other /api/* routes: require a valid bearer token.
        - No token → 401 {"error": "Missing token"}
        - Invalid token → 401 {"error": "Invalid token"}
        - Agent token on REST route → 403 {"error": "Agent tokens cannot access REST API"}
        - Valid viewer/operator token → request.state.role set accordingly.
    """

    def __init__(self, app: Any, auth_config: AuthConfig) -> None:
        super().__init__(app)
        self._auth = auth_config

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Skip auth for non-API routes (static files, dashboard SPA, etc.)
        if not path.startswith("/api/"):
            request.state.role = None
            return await call_next(request)

        # /api/health is exempt — liveness probes must work without credentials.
        if path == "/api/health":
            request.state.role = None
            return await call_next(request)

        # /api/agent/* — WebSocket endpoint handles first-message auth.
        # HTTP middleware passes through; WS auth is enforced by authenticate_ws().
        if path.startswith(_AGENT_PATH_PREFIX):
            auth_header = request.headers.get("authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if token:
                role = self._auth.validate_token(token)
                request.state.role = role
                logger.debug(
                    "auth.agent_path path=%s role=%s valid=%s",
                    path,
                    role,
                    role is not None,
                )
            else:
                request.state.role = None
            return await call_next(request)

        # All other /api/* routes require a valid human (viewer/operator) token.
        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()

        if not token:
            logger.warning("auth.missing_token path=%s", path)
            return JSONResponse(
                {"error": "Missing token"}, status_code=401
            )

        role = self._auth.validate_token(token)

        if role is None:
            logger.warning("auth.invalid_token path=%s", path)
            return JSONResponse(
                {"error": "Invalid token"}, status_code=401
            )

        # Agent tokens are for WebSocket connections only — block on REST (ASI03).
        if role == "agent":
            logger.warning("auth.agent_token_on_rest path=%s", path)
            return JSONResponse(
                {"error": "Agent tokens cannot access REST API"},
                status_code=403,
            )

        request.state.role = role
        logger.debug("auth.ok path=%s role=%s", path, role)
        return await call_next(request)
