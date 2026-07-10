"""Embedded messaging-service wiring (COMP-004 / REQ-090).

The bug this covers: ``team_chat`` routes read
``request.app.state.messaging_service`` but nothing on a live deployment
ever set it, so ``/api/team/channels`` failed open to ``{"channels": []}``
while ``arc team channels`` listed real channels. These tests drive the
real Starlette app: the lifespan must build the service from arcteam
primitives when a ``team_root`` is present, and the routes must surface an
explicit ``team_messaging_unavailable`` error — never a fabricated empty
list — when no service is wired.

NATS is never stood up here. ``arcui.messaging._connect_backend`` is the
same monkeypatch seam arccli's ``_build_service`` uses: tests inject an
in-memory backend so the construction path is exercised end-to-end without
a broker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

import arcui.messaging as messaging
from arcui.auth import AuthConfig
from arcui.server import create_app

_SIGNER_SEED = b"\x11" * 32


async def _staged_backend() -> MemoryBackend:
    """MemoryBackend with one real channel, staged through a seed service."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(_SIGNER_SEED))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    seed = MessagingService(backend, registry, audit)
    await seed.create_channel(Channel(name="work", description="fleet work", members=[]))
    return backend


def _app_with_team_root(tmp_path: Path) -> Any:
    team_root = tmp_path / "team"
    team_root.mkdir()
    auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": "operator-tok"})
    return create_app(
        auth_config=auth,
        team_root=team_root,
        data_dir=tmp_path / "data",
    )


class TestLifespanWiring:
    async def test_lifespan_builds_service_and_lists_real_channels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = await _staged_backend()

        async def fake_connect() -> MemoryBackend:
            return backend

        monkeypatch.setattr(messaging, "_connect_backend", fake_connect)
        monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_SIGNER_SEED))

        app = _app_with_team_root(tmp_path)
        with TestClient(app) as client:
            assert app.state.messaging_service is not None
            resp = client.get(
                "/api/team/channels",
                headers={"Authorization": "Bearer viewer-tok"},
            )
            assert resp.status_code == 200
            names = [c["name"] for c in resp.json()["channels"]]
            assert "work" in names

    async def test_service_closed_on_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []

        class ClosableBackend(MemoryBackend):
            async def close(self) -> None:
                closed.append(True)

        backend = ClosableBackend()
        audit = AuditLogger(backend, InProcessSigner(_SIGNER_SEED))
        await audit.initialize()

        async def fake_connect() -> ClosableBackend:
            return backend

        monkeypatch.setattr(messaging, "_connect_backend", fake_connect)
        monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_SIGNER_SEED))

        app = _app_with_team_root(tmp_path)
        with TestClient(app):
            pass
        assert closed == [True]


class TestExplicitUnavailable:
    def test_channels_missing_service_returns_error_not_empty(self, tmp_path: Path) -> None:
        auth = AuthConfig({"viewer_token": "viewer-tok"})
        app = create_app(auth_config=auth, data_dir=tmp_path / "data")
        client = TestClient(app)
        resp = client.get(
            "/api/team/channels",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body == {"error": "team_messaging_unavailable"}
        assert body != {"channels": []}

    def test_channel_messages_missing_service_returns_error_not_empty(
        self, tmp_path: Path
    ) -> None:
        auth = AuthConfig({"viewer_token": "viewer-tok"})
        app = create_app(auth_config=auth, data_dir=tmp_path / "data")
        client = TestClient(app)
        resp = client.get(
            "/api/team/channels/work/messages",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 503
        assert resp.json() == {"error": "team_messaging_unavailable"}


class TestBuilder:
    async def test_build_returns_none_when_operator_key_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-safe: no audit authority => no service (routes degrade to an
        # explicit error) rather than a service on a wrong-key audit chain.
        monkeypatch.setattr(messaging, "_operator_signer", lambda: None)
        service, backend = await messaging.build_messaging_service(backend=MemoryBackend())
        assert service is None
        assert backend is None

    async def test_build_over_injected_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_SIGNER_SEED))
        backend = await _staged_backend()
        service, returned = await messaging.build_messaging_service(backend=backend)
        assert service is not None
        assert returned is backend
        channels = await service.list_channels()
        assert [c.name for c in channels] == ["work"]
