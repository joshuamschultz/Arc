"""Integration tests for the Team Chat routes.

Read-only window into arcteam channels for ArcUI's Team Chat tab.
Both endpoints fail-open (empty payload) when no MessagingService is
configured, which is what tests without a team_root expect.
"""

from __future__ import annotations

import pytest
from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


@pytest.fixture
async def svc_with_channel() -> MessagingService:
    """MessagingService with one channel, two registered agents, three
    messages already sent — enough for the routes to surface real
    payloads without each test re-staging the world."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)

    for entity_id, name, handle in (
        ("agent://intake", "Intake", "intake"),
        ("agent://architect", "Architect", "architect"),
    ):
        await registry.register(
            Entity(
                did=f"did:arc:local:agent/{handle}",
                handle=handle,
                id=entity_id,
                name=name,
                type=EntityType.AGENT,
            )
        )

    await svc.create_channel(
        Channel(
            name="access-review-REQ-001",
            description="AccessGuard demo run",
            members=["agent://intake", "agent://architect"],
        )
    )
    for body, sender in (
        ("normalised intake", "agent://intake"),
        ("issued grants list", "agent://architect"),
        ("redteam strikes accepted", "agent://intake"),
    ):
        await svc.send(
            Message(
                sender=sender,
                to=["channel://access-review-REQ-001"],
                body=body,
            )
        )
    return svc


def _make_client(svc: MessagingService | None = None) -> TestClient:
    auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": "operator-tok"})
    app = create_app(auth_config=auth, messaging_service=svc)
    return TestClient(app)


class TestListChannels:
    def test_returns_empty_when_no_service(self) -> None:
        client = _make_client(None)
        resp = client.get(
            "/api/team/channels",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"channels": []}

    async def test_returns_channels_when_service_present(
        self, svc_with_channel: MessagingService
    ) -> None:
        client = _make_client(svc_with_channel)
        resp = client.get(
            "/api/team/channels",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        names = [c["name"] for c in payload["channels"]]
        assert "access-review-REQ-001" in names


class TestChannelMessages:
    def test_returns_empty_when_no_service(self) -> None:
        client = _make_client(None)
        resp = client.get(
            "/api/team/channels/anything/messages",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"] == []
        assert body["channel"] == "anything"
        assert body["next_after_seq"] is None

    @pytest.mark.parametrize(
        "bad_name",
        [
            "name with spaces",
            "name;DROP",
            "name<script>",
        ],
    )
    def test_rejects_unsafe_channel_names(self, bad_name: str) -> None:
        client = _make_client(None)
        resp = client.get(
            f"/api/team/channels/{bad_name}/messages",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        # Either our regex rejects (400) or starlette's router refused
        # to match. Both keep the unsafe value out of the backend.
        assert resp.status_code in (400, 404)

    async def test_returns_chronological_messages(
        self, svc_with_channel: MessagingService
    ) -> None:
        client = _make_client(svc_with_channel)
        resp = client.get(
            "/api/team/channels/access-review-REQ-001/messages",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        bodies = [m["body"] for m in resp.json()["messages"]]
        assert bodies == [
            "normalised intake",
            "issued grants list",
            "redteam strikes accepted",
        ]

    async def test_limit_clamps_and_after_seq_paginates(
        self, svc_with_channel: MessagingService
    ) -> None:
        client = _make_client(svc_with_channel)
        first = client.get(
            "/api/team/channels/access-review-REQ-001/messages?limit=2",
            headers={"Authorization": "Bearer viewer-tok"},
        ).json()
        assert len(first["messages"]) == 2
        assert first["next_after_seq"] == first["messages"][-1]["seq"]

        rest = client.get(
            "/api/team/channels/access-review-REQ-001/messages"
            f"?after_seq={first['next_after_seq']}&limit=10",
            headers={"Authorization": "Bearer viewer-tok"},
        ).json()
        assert [m["body"] for m in rest["messages"]] == ["redteam strikes accepted"]
        assert rest["next_after_seq"] is None
