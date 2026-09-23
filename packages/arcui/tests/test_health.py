"""Public liveness and safe dependency readiness contracts."""

from __future__ import annotations

from arcstore.backends.memory import FakeBackend
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.server import _health, _ready, create_app


class _Broker:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    async def check_ready(self) -> bool:
        return self.ready


def _app(*, fleet: bool, broker: bool, messaging: bool) -> Starlette:
    app = Starlette(routes=[Route("/api/health", _health), Route("/api/ready", _ready)])
    app.add_middleware(
        AuthMiddleware, auth_config=AuthConfig({"viewer_token": "v", "operator_token": "o"})
    )
    app.state.requires_fleet = fleet
    app.state.broker = _Broker(broker) if fleet else None
    app.state.messaging_service = object() if messaging else None
    app.state.store_started = True
    return app


def test_standalone_ready_without_optional_fleet() -> None:
    with TestClient(_app(fleet=False, broker=False, messaging=False)) as client:
        assert client.get("/api/ready").status_code == 200


def test_configured_fleet_failure_is_publicly_degraded_without_details() -> None:
    with TestClient(_app(fleet=True, broker=False, messaging=False)) as client:
        assert client.get("/api/health").status_code == 200
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "degraded",
            "components": {"store": "ready", "fleet": "unavailable"},
        }


def test_configured_workflow_requires_live_runner_and_control_plane() -> None:
    app = _app(fleet=True, broker=True, messaging=True)
    app.state.requires_workflow = True
    app.state.workflow_runner_host = type("Host", (), {"available": False})()
    app.state.workflow_control_plane = None
    with TestClient(app) as client:
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["components"]["workflow"] == "unavailable"


def test_store_failed_start_retries_and_readiness_recovers() -> None:
    import time

    class _FlakyBackend(FakeBackend):
        calls = 0

        async def start(self) -> None:
            self.calls += 1
            if self.calls <= 2:
                raise OSError("temporary store outage")
            await super().start()

    backend = _FlakyBackend()
    app = create_app(arcstore_backend=backend)
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 503
        for _ in range(20):
            if client.get("/api/ready").status_code == 200:
                break
            time.sleep(0.05)
        assert client.get("/api/ready").status_code == 200
    assert backend.calls >= 2
