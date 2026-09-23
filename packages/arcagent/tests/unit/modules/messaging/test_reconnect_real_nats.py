"""A configured fleet inbox joins NATS after an initial broker outage."""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from arcteam import (
    Channel,
    Entity,
    EntityType,
    FleetBackendUnavailableError,
    Message,
    MessagingService,
    composition,
)
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.modules.messaging import _runtime, capabilities

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server not installed"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _until_ready() -> None:
    while not _runtime.state().live_backend_ready:
        await asyncio.sleep(0.05)


async def _until_unready() -> None:
    while _runtime.state().live_backend_ready:
        await asyncio.sleep(0.05)


async def test_initial_outage_recovers_signed_direct_and_group_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One running agent joins its durable inbox after the broker comes online."""
    port = _free_port()
    url = f"nats://127.0.0.1:{port}"
    self_identity = AgentIdentity.generate(org="local", agent_type="agent")
    peer_identity = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="Me", nats_url=url),
        workspace=tmp_path,
        identity=self_identity,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    with pytest.raises(FleetBackendUnavailableError):
        await st.svc.list_channels()

    failed = asyncio.Event()
    real_make_backend = composition.make_backend

    async def counted_connect(configured_url: str) -> Any:
        try:
            return await real_make_backend(configured_url)
        except FleetBackendUnavailableError:
            failed.set()
            raise

    monkeypatch.setattr(composition, "make_backend", counted_connect)
    received: list[Message] = []

    async def capture(message: Message) -> None:
        received.append(message)

    monkeypatch.setattr(capabilities, "_handle_incoming", capture)
    inbox = asyncio.create_task(capabilities.messaging_inbox_loop(None))
    server: subprocess.Popen[bytes] | None = None
    try:
        await asyncio.wait_for(failed.wait(), timeout=8)
        assert not st.live_backend_ready
        binary = shutil.which("nats-server")
        assert binary is not None
        server = subprocess.Popen(
            [binary, "-js", "-p", str(port), "-sd", str(tmp_path / "jetstream")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.wait_for(_until_ready(), timeout=10)
        assert st.live_subscription is not None

        await st.registry.register(
            Entity(
                did=self_identity.did,
                handle="me",
                id="agent://me",
                name="Me",
                type=EntityType.AGENT,
                public_key=self_identity.public_key.hex(),
            )
        )
        await st.registry.register(
            Entity(
                did=peer_identity.did,
                handle="peer",
                id="agent://peer",
                name="Peer",
                type=EntityType.AGENT,
                public_key=peer_identity.public_key.hex(),
            )
        )
        await st.svc.create_channel(
            Channel(name="recovery", members=["agent://me", "agent://peer"])
        )
        await st.svc.refresh_subscription("agent://me", st.live_subscription)
        sender = MessagingService(
            st.live_backend,
            st.registry,
            st.svc._audit,
            signer=composition.message_signer(peer_identity),
        )
        await sender.send(Message(sender="agent://peer", to=["agent://me"], body="direct"))
        await sender.send(Message(sender="agent://peer", to=["channel://recovery"], body="group"))
        async with asyncio.timeout(8):
            while len(received) < 2:
                await asyncio.sleep(0.05)
        assert {message.body for message in received} == {"direct", "group"}
        assert all(message.signer_did == peer_identity.did and message.sig for message in received)

        server.terminate()
        server.wait(timeout=5)
        server = None
        await asyncio.wait_for(_until_unready(), timeout=8)
        server = subprocess.Popen(
            [binary, "-js", "-p", str(port), "-sd", str(tmp_path / "jetstream")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.wait_for(_until_ready(), timeout=10)
        await sender.send(Message(sender="agent://peer", to=["agent://me"], body="after-restart"))
        async with asyncio.timeout(8):
            while len(received) < 3:
                await asyncio.sleep(0.05)
        assert received[-1].body == "after-restart"
    finally:
        inbox.cancel()
        try:
            await inbox
        except asyncio.CancelledError:
            pass
        if server is not None:
            server.terminate()
            server.wait(timeout=5)
        _runtime.reset()
    assert not st.live_backend_ready
