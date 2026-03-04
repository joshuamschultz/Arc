"""AuthMiddleware — bearer token authentication with viewer/operator roles.

Token → role mapping is configured at app startup. Auto-generates a viewer
token if none provided.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


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

    Sets request.state.role to "viewer", "operator", or None.
    Returns 401 for missing/invalid tokens on protected routes.
    """

    def __init__(self, app: Any, auth_config: AuthConfig) -> None:
        super().__init__(app)
        self._auth = auth_config

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for non-API routes (static files, etc)
        if not request.url.path.startswith("/api/"):
            request.state.role = None
            return await call_next(request)

        # Health endpoint is public (load balancers, k8s probes)
        if request.url.path == "/api/health":
            request.state.role = None
            return await call_next(request)

        # Extract bearer token
        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()

        if not token:
            return JSONResponse(
                {"error": "Missing authorization token"}, status_code=401
            )

        role = self._auth.validate_token(token)
        if role is None:
            return JSONResponse(
                {"error": "Invalid authorization token"}, status_code=401
            )

        # Agent tokens are only valid for WebSocket endpoints, not REST API
        if role == "agent" and not request.url.path.startswith("/api/agent/"):
            return JSONResponse(
                {"error": "Agent tokens cannot access REST API"}, status_code=403
            )

        request.state.role = role
        return await call_next(request)
