"""H-001b end-to-end: a real signed channel send reaches Home's NEEDS-YOU panel.

Not a hand-built row. This drives the exact path a woken agent turn takes when
it calls ``messaging_send(to="channel://…", action_required=True)``: a real
:class:`~arcteam.messenger.MessagingService` signs the envelope on send, stores
it on the channel stream, and the ``waiting_on_human`` reader behind
``GET /api/home/needs`` picks it up. It proves the whole wire — signer →
messenger storage → reader → route — and that the ``action_required`` gate is
what fills the queue: an unflagged auto-reply or statement sent the same way
does not surface.
"""

from __future__ import annotations

import asyncio

from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message
from arctrust import generate_keypair
from arctrust.signer import InProcessSigner
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.home import routes as home_routes

_AGENT_DID = "did:arc:local:agent/olivia"
_HUMAN_DID = "did:arc:local:user/operator"
_VIEWER = {"Authorization": "Bearer viewer"}


async def _real_service() -> tuple[MessagingService, EntityRegistry]:
    """A live signing MessagingService with a registered agent, operator, channel."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    kp = generate_keypair()
    await registry.register(
        Entity(
            did=_AGENT_DID,
            handle="olivia",
            id="agent://olivia",
            name="Olivia",
            type=EntityType.AGENT,
            public_key=kp.public_key.hex(),
        )
    )
    await registry.register(
        Entity(
            did=_HUMAN_DID,
            handle="operator",
            id="user://operator",
            name="Operator",
            type=EntityType.USER,
        )
    )
    signer = MessageSigner(did=_AGENT_DID, private_key=kp.private_key)
    svc = MessagingService(backend, registry, audit, signer=signer)
    await svc.create_channel(Channel(name="ops", members=[_AGENT_DID, _HUMAN_DID]))
    return svc, registry


async def _seed(sends: list[tuple[str, bool]]) -> tuple[MessagingService, EntityRegistry]:
    """Send each ``(body, action_required)`` through the REAL signed send path."""
    svc, registry = await _real_service()
    for body, action_required in sends:
        await svc.send(
            Message(
                sender="agent://olivia",
                to=["channel://ops"],
                body=body,
                action_required=action_required,
            )
        )
    return svc, registry


def _app(svc: MessagingService, registry: EntityRegistry) -> Starlette:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=home_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.approval_store = None
    app.state.observe = None
    app.state.roster_provider = lambda: []
    app.state.messaging_service = svc
    app.state.messaging_registry = registry
    return app


def test_flagged_ask_surfaces_through_real_send_path() -> None:
    svc, registry = asyncio.run(_seed([("Approve the production deploy?", True)]))
    resp = TestClient(_app(svc, registry)).get("/api/home/needs", headers=_VIEWER)

    assert resp.status_code == 200
    body = resp.json()
    assert body["waiting_on_human"]["count"] == 1
    item = body["waiting_on_human"]["items"][0]
    assert item["agent_did"] == _AGENT_DID  # the signed asker, end to end
    assert item["channel"] == "ops"
    assert "Approve the production deploy?" in item["preview"]
    assert body["total"] == 1


def test_unflagged_messages_are_excluded_through_real_send_path() -> None:
    # An auto-posted closing reply and a plain FYI, both sent the real way with
    # action_required False — neither is an operator action, so the queue is empty.
    svc, registry = asyncio.run(
        _seed([("Done, report attached.", False), ("FYI I started the migration.", False)])
    )
    resp = TestClient(_app(svc, registry)).get("/api/home/needs", headers=_VIEWER)

    body = resp.json()
    assert body["waiting_on_human"] == {"count": 0, "items": []}
    assert body["total"] == 0


def test_only_the_flagged_ask_surfaces_when_mixed() -> None:
    svc, registry = asyncio.run(
        _seed(
            [
                ("Working on it now.", False),
                ("Which region should I deploy to?", True),
            ]
        )
    )
    resp = TestClient(_app(svc, registry)).get("/api/home/needs", headers=_VIEWER)

    body = resp.json()
    assert body["waiting_on_human"]["count"] == 1
    assert "Which region" in body["waiting_on_human"]["items"][0]["preview"]
