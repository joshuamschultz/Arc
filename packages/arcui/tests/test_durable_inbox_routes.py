"""Authenticated durable-inbox route coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from arcgateway.team_roster import RosterEntry
from arcstore.inbox import Handoff, Message, ParticipantRole
from arcstore.inbox_projection import DurableInboxService, participant
from arcteam.mail import AgentMailService
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.agent_detail import routes

_AGENT_DID = "did:arc:test:agent"
_HUMAN_DID = "did:arc:test:human"


class _DeliveryPort:
    async def deliver_reply(self, _message: Message) -> None:
        pass

    async def wake_handoff(self, _handoff: Handoff) -> None:
        pass

    async def deliver_handoff_resolution(self, _handoff: Handoff) -> None:
        pass


class _MailTransport:
    async def send(self, message: object) -> object:
        return message


def _app() -> tuple[Starlette, AuthConfig, DurableInboxService]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    service = DurableInboxService(FakeInboxRepository(), delivery_port=_DeliveryPort())
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.inbox_service = service
    app.state.agent_mail = AgentMailService(_MailTransport(), service)
    app.state.inbox_clearance = "UNCLASSIFIED"
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="alpha",
            name="alpha",
            did=_AGENT_DID,
            org=None,
            type=None,
            workspace_path=str(Path("/nonexistent")),
            model=None,
            provider=None,
            online=True,
            display_name="alpha",
            color="#000000",
            role_label="",
            hidden=False,
        )
    ]
    return app, auth, service


def _headers(auth: AuthConfig, role: str) -> dict[str, str]:
    token = auth.operator_token if role == "operator" else auth.viewer_token
    return {"Authorization": f"Bearer {token}"}


def _seed(service: DurableInboxService) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        await service.record_event(
            event_id="event-1",
            sender=participant(_HUMAN_DID, role=ParticipantRole.HUMAN),
            recipients=(participant(_AGENT_DID),),
            body="Can you review this?",
            external_thread_id="platform-thread-1",
        )
        _, threads, _ = await service.list_threads(participant(_AGENT_DID))
        page = await service.list_messages(threads[0].thread_id, reader=participant(_AGENT_DID))
        return threads[0].thread_id, page.items[0].message_id

    return asyncio.run(run())


def test_inbox_routes_authenticate_and_support_thread_reply_read_and_handoff() -> None:
    app, auth, service = _app()
    thread_id, message_id = _seed(service)
    client = TestClient(app)
    base = "/api/agents/alpha/inbox"

    assert client.get(base).status_code == 401
    listed = client.get(base, headers=_headers(auth, "viewer"))
    assert listed.status_code == 200
    assert [thread["thread_id"] for thread in listed.json()["threads"]] == [thread_id]
    thread = client.get(f"{base}/{thread_id}", headers=_headers(auth, "viewer"))
    assert thread.status_code == 200
    assert [message["message_id"] for message in thread.json()["messages"]] == [message_id]

    reply_path = f"{base}/{thread_id}/reply"
    assert (
        client.post(
            reply_path, json={"body": "Reviewed."}, headers=_headers(auth, "viewer")
        ).status_code
        == 403
    )
    reply = client.post(
        reply_path,
        json={"body": "Reviewed.", "reply_to_id": message_id},
        headers={**_headers(auth, "operator"), "Idempotency-Key": "reply-1"},
    )
    assert reply.status_code == 201
    assert reply.json()["message"]["reply_to_id"] == message_id

    read = client.post(f"{base}/messages/{message_id}/read", headers=_headers(auth, "operator"))
    assert read.status_code == 200
    handoff = client.post(
        f"{base}/{thread_id}/handoffs",
        json={"to": [_HUMAN_DID], "source_message_id": message_id},
        headers={**_headers(auth, "operator"), "Idempotency-Key": "handoff-1"},
    )
    assert handoff.status_code == 201
    updated = client.get(f"{base}/{thread_id}", headers=_headers(auth, "viewer"))
    assert len(updated.json()["messages"]) == 2
    assert len(updated.json()["handoffs"]) == 1
