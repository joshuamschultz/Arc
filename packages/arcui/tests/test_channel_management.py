"""Channel management routes (COMP-005 / REQ-091, REQ-092).

Operator-gated create + membership mutations wired to the same arcteam
MessagingService the read routes use, through the lifespan builder over an
in-memory backend (no NATS). Every mutation is operator-only (403 for
viewer), audited via the COMP-010 helper, and unavailable-when-unwired the
same way the reads are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

import arcui.messaging as messaging
from arcui.auth import AuthConfig
from arcui.server import create_app

_SIGNER_SEED = b"\x11" * 32
_INTAKE_DID = "did:arc:local:agent/intake"
_ARCHITECT_DID = "did:arc:local:agent/architect"


async def _staged_backend() -> MemoryBackend:
    """MemoryBackend with two registered agents, no channels yet."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(_SIGNER_SEED))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    for did, handle, name in (
        (_INTAKE_DID, "intake", "Intake"),
        (_ARCHITECT_DID, "architect", "Architect"),
    ):
        await registry.register(
            Entity(
                did=did,
                handle=handle,
                id=f"agent://{handle}",
                name=name,
                type=EntityType.AGENT,
            )
        )
    return backend


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = await _staged_backend()

    async def fake_connect() -> MemoryBackend:
        return backend

    monkeypatch.setattr(messaging, "_connect_backend", fake_connect)
    monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_SIGNER_SEED))

    team_root = tmp_path / "team"
    team_root.mkdir()
    auth = AuthConfig({"viewer_token": "view-tok", "operator_token": "op-tok"})
    app = create_app(auth_config=auth, team_root=team_root, data_dir=tmp_path / "data")
    with TestClient(app) as c:
        yield c


def _op(headers: dict[str, str] | None = None) -> dict[str, str]:
    return {"Authorization": "Bearer op-tok", **(headers or {})}


def _viewer() -> dict[str, str]:
    return {"Authorization": "Bearer view-tok"}


class TestCreateChannel:
    def test_operator_creates_channel(self, client: TestClient) -> None:
        resp = client.post(
            "/api/team/channels",
            headers=_op(),
            json={"name": "work", "members": ["agent://intake"]},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "work"
        assert _INTAKE_DID in resp.json()["members"]

        listed = client.get("/api/team/channels", headers=_viewer()).json()
        assert "work" in [c["name"] for c in listed["channels"]]

    def test_duplicate_name_conflicts(self, client: TestClient) -> None:
        client.post("/api/team/channels", headers=_op(), json={"name": "work", "members": []})
        resp = client.post(
            "/api/team/channels", headers=_op(), json={"name": "work", "members": []}
        )
        assert resp.status_code == 409

    def test_viewer_forbidden(self, client: TestClient) -> None:
        resp = client.post(
            "/api/team/channels", headers=_viewer(), json={"name": "secret", "members": []}
        )
        assert resp.status_code == 403
        # The mutation must not have happened.
        listed = client.get("/api/team/channels", headers=_viewer()).json()
        assert "secret" not in [c["name"] for c in listed["channels"]]

    def test_invalid_name_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/team/channels", headers=_op(), json={"name": "bad name!", "members": []}
        )
        assert resp.status_code == 400

    def test_audits_create(self, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("INFO", logger="arcui.audit"):
            client.post("/api/team/channels", headers=_op(), json={"name": "work", "members": []})
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "channel.create"
            and e["details"]["outcome"] == "applied"
            and e["details"]["target"] == "channel://work"
            for e in events
        )


class TestMembership:
    def test_operator_adds_member(self, client: TestClient) -> None:
        client.post("/api/team/channels", headers=_op(), json={"name": "work", "members": []})
        resp = client.post(
            "/api/team/channels/work/members",
            headers=_op(),
            json={"member": "agent://architect"},
        )
        assert resp.status_code == 200
        assert resp.json()["member"] == _ARCHITECT_DID

        listed = client.get("/api/team/channels", headers=_viewer()).json()
        work = next(c for c in listed["channels"] if c["name"] == "work")
        assert _ARCHITECT_DID in work["members"]

    def test_operator_removes_member(self, client: TestClient) -> None:
        client.post(
            "/api/team/channels",
            headers=_op(),
            json={"name": "work", "members": ["agent://architect"]},
        )
        resp = client.request(
            "DELETE",
            "/api/team/channels/work/members",
            headers=_op(),
            json={"member": "agent://architect"},
        )
        assert resp.status_code == 200

        listed = client.get("/api/team/channels", headers=_viewer()).json()
        work = next(c for c in listed["channels"] if c["name"] == "work")
        assert _ARCHITECT_DID not in work["members"]

    def test_viewer_forbidden(self, client: TestClient) -> None:
        client.post("/api/team/channels", headers=_op(), json={"name": "work", "members": []})
        resp = client.post(
            "/api/team/channels/work/members",
            headers=_viewer(),
            json={"member": "agent://architect"},
        )
        assert resp.status_code == 403

    def test_audits_member_did(self, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
        client.post("/api/team/channels", headers=_op(), json={"name": "work", "members": []})
        with caplog.at_level("INFO", logger="arcui.audit"):
            client.post(
                "/api/team/channels/work/members",
                headers=_op(),
                json={"member": "agent://architect"},
            )
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        # REQ-092: actor, channel, and member DID recorded.
        assert any(
            e["details"]["operation"] == "channel.member_add"
            and e["details"]["target"] == "channel://work"
            and _ARCHITECT_DID in e["details"]["detail"]
            for e in events
        )


class TestUnavailable:
    def test_create_without_service_is_503(self, tmp_path: Path) -> None:
        auth = AuthConfig({"operator_token": "op-tok"})
        app = create_app(auth_config=auth, data_dir=tmp_path / "data")
        c = TestClient(app)
        resp = c.post(
            "/api/team/channels",
            headers={"Authorization": "Bearer op-tok"},
            json={"name": "work", "members": []},
        )
        assert resp.status_code == 503
        assert resp.json() == {"error": "team_messaging_unavailable"}
