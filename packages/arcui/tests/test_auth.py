"""Tests for AuthMiddleware — bearer token validation and role assignment."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware


def _make_app(auth_config: AuthConfig) -> Starlette:
    """Create a test app with auth middleware."""

    async def protected(request: Request) -> JSONResponse:
        return JSONResponse({"role": request.state.role})

    async def public(request: Request) -> JSONResponse:
        return JSONResponse({"public": True})

    app = Starlette(
        routes=[
            Route("/api/test", protected),
            Route("/health", public),
        ]
    )
    app.add_middleware(AuthMiddleware, auth_config=auth_config)
    return app


class TestAuthConfig:
    def test_auto_generates_tokens(self) -> None:
        cfg = AuthConfig()
        assert len(cfg.viewer_token) == 64  # secrets.token_hex(32) = 64 hex chars
        assert len(cfg.operator_token) == 64
        assert cfg.viewer_token != cfg.operator_token

    def test_uses_provided_tokens(self) -> None:
        cfg = AuthConfig(
            {
                "viewer_token": "v-token",
                "operator_token": "o-token",
            }
        )
        assert cfg.viewer_token == "v-token"
        assert cfg.operator_token == "o-token"

    def test_validate_viewer_token(self) -> None:
        cfg = AuthConfig({"viewer_token": "abc", "operator_token": "xyz"})
        assert cfg.validate_token("abc") == "viewer"

    def test_validate_operator_token(self) -> None:
        cfg = AuthConfig({"viewer_token": "abc", "operator_token": "xyz"})
        assert cfg.validate_token("xyz") == "operator"

    def test_validate_invalid_token(self) -> None:
        cfg = AuthConfig({"viewer_token": "abc", "operator_token": "xyz"})
        assert cfg.validate_token("bad") is None


class TestAuthMiddleware:
    def test_valid_viewer_token(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = _make_app(auth)
        client = TestClient(app)

        resp = client.get("/api/test", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"

    def test_valid_operator_token(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = _make_app(auth)
        client = TestClient(app)

        resp = client.get("/api/test", headers={"Authorization": "Bearer o"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "operator"

    def test_missing_token_returns_401(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = _make_app(auth)
        client = TestClient(app)

        resp = client.get("/api/test")
        assert resp.status_code == 401
        assert "Missing" in resp.json()["error"]

    def test_invalid_token_returns_401(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = _make_app(auth)
        client = TestClient(app)

        resp = client.get("/api/test", headers={"Authorization": "Bearer bad"})
        assert resp.status_code == 401
        assert "Invalid" in resp.json()["error"]

    def test_non_api_routes_skip_auth(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = _make_app(auth)
        client = TestClient(app)

        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["public"] is True

    def test_health_endpoint_exempt_from_auth(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})

        async def health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok"})

        async def test_route(request: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/api/health", health), Route("/api/test", test_route)])
        app.add_middleware(AuthMiddleware, auth_config=auth)
        client = TestClient(app)

        # Health should work without auth
        resp = client.get("/api/health")
        assert resp.status_code == 200

        # Other API routes still require auth
        resp = client.get("/api/test")
        assert resp.status_code == 401

    def test_health_path_requires_no_token(self) -> None:
        """GET /api/health works with no token for liveness probe compatibility."""
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})

        async def health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[Route("/api/health", health)])
        app.add_middleware(AuthMiddleware, auth_config=auth)
        client = TestClient(app)

        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
