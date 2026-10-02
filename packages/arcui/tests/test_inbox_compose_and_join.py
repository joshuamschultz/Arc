"""Operator compose, operator join into agent threads, and the mail wiring (item 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcstore.inbox import ParticipantRole
from arcstore.inbox_projection import DurableInboxService, thread_id_for
from arcteam.crypto import MessageSigner
from arcteam.mail import AgentMailService, MailSendRequest
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.agent_detail import inbox as inbox_routes
from arcui.routes.agent_detail import routes

_AGENT_DID = "did:arc:test:agent/alpha"
_PEER_DID = "did:arc:test:agent/beta"
_OPERATOR_DID = "did:arc:test:operator"
_PEER_SEED = b"\x33" * 32
_OPERATOR_SEED = b"\x22" * 32

_ADDRESSES = {
    "agent://alpha": _AGENT_DID,
    "agent://beta": _PEER_DID,
    "user://operator": _OPERATOR_DID,
}


class _Transport:
    async def send(self, message: object) -> object:
        return message


class _Outbox:
    def claim(
        self, _worker_id: str, *, limit: int, signer_did: str | None = None
    ) -> tuple[object, ...]:
        return ()


class _Book:
    async def did_for(self, address: str) -> str:
        return address if address.startswith("did:") else _ADDRESSES[address]

    async def address_for(self, did: str) -> str:
        return next(address for address, value in _ADDRESSES.items() if value == did)


def _mail(service: DurableInboxService, did: str, seed: bytes) -> AgentMailService:
    return AgentMailService(
        _Transport(),
        service,
        outbox=_Outbox(),
        address_book=_Book(),
        signer=MessageSigner(did=did, private_key=seed),
    )


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def record(_request: Any, **fields: Any) -> None:
        recorded.append(fields)

    monkeypatch.setattr(inbox_routes, "emit_mutation_audit", record)
    return recorded


def _app() -> tuple[Starlette, AuthConfig, DurableInboxService]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    service = DurableInboxService(FakeInboxRepository())
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.inbox_service = service
    app.state.agent_mail = _mail(service, _OPERATOR_DID, _OPERATOR_SEED)
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


def _operator(auth: AuthConfig, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {auth.operator_token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_viewer_cannot_compose_and_the_denial_is_audited(audits: list[dict[str, Any]]) -> None:
    app, auth, _service = _app()
    client = TestClient(app)

    response = client.post(
        "/api/agents/alpha/inbox",
        json={"subject": "Hi", "body": "Hello"},
        headers={"Authorization": f"Bearer {auth.viewer_token}", "Idempotency-Key": "k"},
    )

    assert response.status_code == 403
    assert audits[-1]["operation"] == "inbox.compose"
    assert audits[-1]["outcome"] == "denied"


@pytest.mark.parametrize(
    "payload",
    [{"body": ""}, {"body": 7}, {"subject": 3, "body": "x"}, ["not", "an", "object"]],
)
def test_compose_rejects_a_bad_body(payload: Any, audits: list[dict[str, Any]]) -> None:
    app, auth, _service = _app()

    response = TestClient(app).post(
        "/api/agents/alpha/inbox", json=payload, headers=_operator(auth, "k")
    )

    assert response.status_code == 400


def test_compose_requires_an_idempotency_key(audits: list[dict[str, Any]]) -> None:
    app, auth, _service = _app()

    response = TestClient(app).post(
        "/api/agents/alpha/inbox", json={"body": "Hello"}, headers=_operator(auth)
    )

    assert response.status_code == 400


def test_compose_to_an_unknown_agent_is_404(audits: list[dict[str, Any]]) -> None:
    app, auth, _service = _app()

    response = TestClient(app).post(
        "/api/agents/ghost/inbox", json={"body": "Hello"}, headers=_operator(auth, "k")
    )

    assert response.status_code == 404


def test_operator_compose_lands_in_the_agent_inbox_once(audits: list[dict[str, Any]]) -> None:
    app, auth, _service = _app()
    client = TestClient(app)
    payload = {"subject": "Q3 close", "body": "Please own the Q3 close."}

    first = client.post("/api/agents/alpha/inbox", json=payload, headers=_operator(auth, "c-1"))
    replay = client.post("/api/agents/alpha/inbox", json=payload, headers=_operator(auth, "c-1"))

    assert first.status_code == 201
    body = first.json()
    assert replay.json()["message_id"] == body["message_id"]
    assert body["thread_id"] == thread_id_for(_AGENT_DID, body["conversation_id"])
    assert body["status"] in {"sent", "pending"}
    listed = client.get("/api/agents/alpha/inbox", headers=_operator(auth))
    threads = listed.json()["threads"]
    assert [thread["subject"] for thread in threads] == ["Q3 close"]
    messages = client.get(f"/api/agents/alpha/inbox/{body['thread_id']}", headers=_operator(auth))
    assert [item["body"] for item in messages.json()["messages"]] == ["Please own the Q3 close."]
    applied = [a for a in audits if a["operation"] == "inbox.compose"]
    assert applied[-1]["outcome"] == "applied"


def _seed_agent_thread(service: DurableInboxService) -> str:
    async def run() -> str:
        sent = await _mail(service, _PEER_DID, _PEER_SEED).send(
            MailSendRequest(
                sender="agent://beta",
                sender_did=_PEER_DID,
                to=("agent://alpha",),
                subject="Data question",
                body="Where is the Q3 file?",
                idempotency_key="peer-1",
            )
        )
        return thread_id_for(_AGENT_DID, sent.thread_id)

    return asyncio.run(run())


def test_operator_can_answer_an_agent_to_agent_thread(audits: list[dict[str, Any]]) -> None:
    app, auth, service = _app()
    thread_id = _seed_agent_thread(service)

    reply = TestClient(app).post(
        f"/api/agents/alpha/inbox/{thread_id}/reply",
        json={"body": "It is in the finance share."},
        headers=_operator(auth, "op-1"),
    )

    assert reply.status_code == 201
    assert reply.json()["message"]["sender"]["participant_id"] == _OPERATOR_DID
    assert reply.json()["message"]["sender"]["role"] == ParticipantRole.HUMAN.value
    applied = [a for a in audits if a["operation"] == "inbox.reply"]
    assert applied[-1]["outcome"] == "applied"


def test_a_second_reply_is_409_and_points_to_the_team_channel(
    audits: list[dict[str, Any]],
) -> None:
    app, auth, service = _app()
    thread_id = _seed_agent_thread(service)
    client = TestClient(app)
    path = f"/api/agents/alpha/inbox/{thread_id}/reply"
    assert client.post(path, json={"body": "one"}, headers=_operator(auth, "a")).status_code == 201

    again = client.post(path, json={"body": "two"}, headers=_operator(auth, "b"))

    assert again.status_code == 409
    assert again.json()["error"] == "mail_thread_closed"
    assert "team channel" in again.json()["detail"]


def test_lifecycle_registers_the_operator_and_binds_the_handoff_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arcteam.audit import AuditLogger
    from arcteam.mail import MailInboxDeliveryPort
    from arcteam.registry import EntityRegistry
    from arcteam.storage import MemoryBackend
    from arctrust.signer import InProcessSigner

    from arcui import messaging as embedded
    from arcui import messaging_lifecycle as lifecycle

    async def scenario() -> None:
        backend = MemoryBackend()
        audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
        await audit.initialize()
        registry = EntityRegistry(backend, audit)
        operator = embedded._OperatorMessaging(
            did=_OPERATOR_DID,
            public_key_hex="00" * 32,
            signer=MessageSigner(did=_OPERATOR_DID, private_key=_OPERATOR_SEED),
        )
        monkeypatch.setattr(embedded, "_operator_messaging", lambda: operator)

        async def build() -> tuple[Any, Any, Any]:
            return SimpleNamespace(), registry, backend

        class _Observer:
            def __init__(self, *_args: Any) -> None:
                pass

            async def run(self, *, interval: float) -> None:
                await asyncio.Event().wait()

        monkeypatch.setattr(lifecycle, "build_messaging_service", build)
        monkeypatch.setattr(lifecycle, "build_team_post_forwarder", lambda **_kwargs: object())
        monkeypatch.setattr(lifecycle, "TeamBusObserver", _Observer)
        port = MailInboxDeliveryPort()
        app = SimpleNamespace(
            state=SimpleNamespace(
                inbox_service=DurableInboxService(FakeInboxRepository(), delivery_port=port),
                inbox_delivery_port=port,
                team_stream=object(),
            )
        )
        owner = lifecycle.MessagingLifecycle(
            app,
            required=True,
            injected_service=None,
            injected_forwarder=None,
            outbox=_Outbox(),
            observer_interval=1.0,
        )
        await owner.start()
        try:
            entity = await registry.get(_OPERATOR_DID)
            assert entity is not None
            assert entity.id == "user://operator"
            assert app.state.agent_mail is not None
            assert port._mail is app.state.agent_mail
        finally:
            await owner.aclose()
        assert port._mail is None

    asyncio.run(scenario())
