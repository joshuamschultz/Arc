"""Operator group-post forwarding through ``/ws/team`` (MSG1 / REQ-061).

The bug this covers: ``arc ui start`` never wired ``team_post_forwarder``, so
every operator post to a channel got a silent ``forward_unavailable`` frame and
the channel looked dead. The lifespan now builds the forwarder from the embedded
MessagingService. These tests drive the real Starlette app end-to-end: an
operator posts over ``/ws/team`` and the message must land in the channel
(readable via ``MessagingService.list_channel_messages``), signed under the
operator's key-derived DID so subscribing agents verify it.

NATS is never stood up — ``arcui.messaging._connect_backend`` is monkeypatched to
an in-memory backend, mirroring ``test_embedded_messaging``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel
from arctrust.identity import did_from_public_key
from arctrust.keypair import KeyPair
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

import arcui.messaging as messaging
from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok"
OPERATOR_TOKEN = "operator-tok"
_AUDIT_SEED = b"\x11" * 32
_OPERATOR_SEED = b"\x22" * 32


def _operator_identity() -> messaging._OperatorMessaging:
    pubkey = KeyPair.from_seed(_OPERATOR_SEED).public_key
    did = did_from_public_key(pubkey, org="local", agent_type="operator")
    return messaging._OperatorMessaging(
        did=did,
        public_key_hex=pubkey.hex(),
        signer=MessageSigner(did=did, private_key=_OPERATOR_SEED),
    )


async def _staged_backend() -> MemoryBackend:
    """MemoryBackend with one real channel, staged through a seed service."""
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(_AUDIT_SEED))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    seed = MessagingService(backend, registry, audit)
    await seed.create_channel(Channel(name="ops", description="fleet ops", members=[]))
    return backend


def _install(monkeypatch: pytest.MonkeyPatch, backend: MemoryBackend) -> None:
    async def fake_connect() -> MemoryBackend:
        return backend

    monkeypatch.setattr(messaging, "_connect_backend", fake_connect)
    monkeypatch.setattr(messaging, "_operator_signer", lambda: InProcessSigner(_AUDIT_SEED))
    monkeypatch.setattr(messaging, "_operator_messaging", _operator_identity)


def _app(tmp_path: Path) -> Any:
    team_root = tmp_path / "team"
    team_root.mkdir()
    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    return create_app(auth_config=auth, team_root=team_root, data_dir=tmp_path / "data")


class TestOperatorPost:
    async def test_post_lands_signed_in_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = await _staged_backend()
        _install(monkeypatch, backend)

        app = _app(tmp_path)
        with TestClient(app) as client:
            assert app.state.team_post_forwarder is not None
            with client.websocket_connect("/ws/team") as ws:
                ws.send_json({"token": OPERATOR_TOKEN})
                assert ws.receive_json()["type"] == "ready"
                ws.send_json({"type": "post", "channel": "ops", "text": "status please"})
                ack = ws.receive_json()
                assert ack["type"] == "posted"
                assert ack["channel"] == "ops"

            service = app.state.messaging_service
            msgs = await service.list_channel_messages("ops")
            assert [m.body for m in msgs] == ["status please"]
            posted = msgs[0]
            # Signed under the operator's own key-bound DID, so agents verify it.
            op = _operator_identity()
            assert posted.sender == op.did
            assert posted.signer_did == op.did
            assert posted.sig != ""

    async def test_operator_self_registers_and_joins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = await _staged_backend()
        _install(monkeypatch, backend)

        app = _app(tmp_path)
        with TestClient(app) as client, client.websocket_connect("/ws/team") as ws:
            ws.send_json({"token": OPERATOR_TOKEN})
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "post", "channel": "ops", "text": "hello"})
            assert ws.receive_json()["type"] == "posted"

            op = _operator_identity()
            registry = app.state.messaging_registry
            entity = await registry.get(op.did)
            assert entity is not None
            assert entity.public_key == op.public_key_hex
            channel = next(c for c in await app.state.messaging_service.list_channels() if c.name == "ops")
            assert op.did in channel.members

    async def test_post_to_new_channel_creates_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = await _staged_backend()
        _install(monkeypatch, backend)

        app = _app(tmp_path)
        with TestClient(app) as client, client.websocket_connect("/ws/team") as ws:
            ws.send_json({"token": OPERATOR_TOKEN})
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "post", "channel": "adhoc", "text": "spin up"})
            assert ws.receive_json()["type"] == "posted"

            service = app.state.messaging_service
            names = [c.name for c in await service.list_channels()]
            assert "adhoc" in names
            msgs = await service.list_channel_messages("adhoc")
            assert [m.body for m in msgs] == ["spin up"]
