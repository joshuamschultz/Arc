"""The dashboard tells the operator when a post cannot be answered (SPEC-068).

Two silences this closes, both of which look exactly like a working post:

* **F4 — an `@handle` naming nobody.** ``apply_mentions`` drops an unresolvable
  handle, so ``@sales_agent`` against a registry holding ``sales`` produces a
  message with no mentions: no inbox fanout, no ``action_required``, no priority
  bump. The operator addressed somebody and silently broadcast instead.
* **F1 — a channel with no agents in it.** Posting auto-creates the channel with
  the operator as its only member, which is a legitimate operator action, but
  the room has nobody in it to reply. Left unsaid, that is indistinguishable
  from six agents choosing not to answer.

The first is refused — the operator can retype the handle. The second is
delivered with a warning, because creating a channel then filling it is a real
workflow and the message is genuinely recorded.
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
from arcteam.types import Channel, Entity, EntityType
from arctrust.identity import did_from_public_key
from arctrust.keypair import KeyPair
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

import arcui.messaging as messaging
from arcui.auth import AuthConfig
from arcui.server import create_app

OPERATOR_TOKEN = "operator-tok"
_AUDIT_SEED = b"\x11" * 32
_OPERATOR_SEED = b"\x22" * 32

DID_SALES = "did:arc:local:agent/sales"

pytestmark = pytest.mark.asyncio


def _operator_identity() -> messaging._OperatorMessaging:
    pubkey = KeyPair.from_seed(_OPERATOR_SEED).public_key
    did = did_from_public_key(pubkey, org="local", agent_type="operator")
    return messaging._OperatorMessaging(
        did=did,
        public_key_hex=pubkey.hex(),
        signer=MessageSigner(did=did, private_key=_OPERATOR_SEED),
    )


async def _backend(*, with_agent_member: bool) -> MemoryBackend:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(_AUDIT_SEED))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    await registry.register(
        Entity(
            did=DID_SALES,
            handle="sales",
            id="agent://sales",
            name="Sales",
            type=EntityType.AGENT,
        )
    )
    seed = MessagingService(backend, registry, audit)
    members = [DID_SALES] if with_agent_member else []
    await seed.create_channel(Channel(name="ops", members=members))
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
    auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": OPERATOR_TOKEN})
    return create_app(auth_config=auth, team_root=team_root, data_dir=tmp_path / "data")


async def test_unknown_mention_is_refused_and_names_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await _backend(with_agent_member=True)
    _install(monkeypatch, backend)

    app = _app(tmp_path)
    with TestClient(app) as client, client.websocket_connect("/ws/team") as ws:
        ws.send_json({"token": OPERATOR_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "channel": "ops", "text": "@sales_agent status?"})
        frame = ws.receive_json()

    assert frame["type"] == "error"
    assert frame["code"] == "post_refused"
    assert "@sales_agent" in frame["message"]

    # Refused means refused: nothing was written to the channel.
    msgs = await app.state.messaging_service.list_channel_messages("ops")
    assert msgs == []


async def test_known_mention_posts_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await _backend(with_agent_member=True)
    _install(monkeypatch, backend)

    app = _app(tmp_path)
    with TestClient(app) as client, client.websocket_connect("/ws/team") as ws:
        ws.send_json({"token": OPERATOR_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "channel": "ops", "text": "@sales status?"})
        frame = ws.receive_json()

    assert frame["type"] == "posted"
    assert not frame.get("warning")
    msgs = await app.state.messaging_service.list_channel_messages("ops")
    assert [m.body for m in msgs] == ["@sales status?"]
    assert msgs[0].mentions == [DID_SALES]


async def test_channel_with_no_agents_warns_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await _backend(with_agent_member=False)
    _install(monkeypatch, backend)

    app = _app(tmp_path)
    with TestClient(app) as client, client.websocket_connect("/ws/team") as ws:
        ws.send_json({"token": OPERATOR_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "channel": "ops", "text": "anyone about?"})
        frame = ws.receive_json()

    assert frame["type"] == "posted"
    assert "ops" in frame["warning"]

    # The post is still recorded — the warning is information, not a refusal.
    msgs = await app.state.messaging_service.list_channel_messages("ops")
    assert [m.body for m in msgs] == ["anyone about?"]


async def test_auto_created_channel_seeds_the_fleet_and_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The F1 case, repaired at the root: a first post to a fresh channel now
    seeds it with the registered fleet, so there is an audience to answer and no
    warning fires. (The warning still guards the genuinely empty-fleet case —
    see :func:`test_channel_with_no_agents_warns_the_operator`.)"""
    backend = await _backend(with_agent_member=True)
    _install(monkeypatch, backend)

    app = _app(tmp_path)
    with TestClient(app) as client, client.websocket_connect("/ws/team") as ws:
        ws.send_json({"token": OPERATOR_TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "post", "channel": "adhoc", "text": "spin up"})
        frame = ws.receive_json()

    assert frame["type"] == "posted"
    assert not frame.get("warning")
    channel = next(
        c for c in await app.state.messaging_service.list_channels() if c.name == "adhoc"
    )
    assert DID_SALES in channel.members
