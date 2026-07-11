"""Channel @mention fan-out to inbox (MSG6 / REQ-004).

A channel @mention must reliably WAKE the mentioned agent even when it is not a
channel member or hasn't subscribed to the channel stream yet — otherwise a
"@josh_agent what is your name?" posted to a channel josh isn't in never reaches
him. ``send`` now fans the envelope into each mentioned entity's inbox
(``arc.agent.{handle}``), which every agent always consumes. DMs (no channel
target) must NOT fan out — an @mention inside a private DM stays private.
"""

from __future__ import annotations

import pytest
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message

pytestmark = pytest.mark.asyncio


async def _svc() -> MessagingService:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit)
    for handle in ("poster", "member", "outsider"):
        await registry.register(
            Entity(
                did=f"did:arc:test:agent/{handle}",
                handle=handle,
                id=f"agent://{handle}",
                name=handle,
                type=EntityType.AGENT,
            )
        )
    # Channel the poster + member belong to; ``outsider`` is deliberately NOT a member.
    await svc.create_channel(Channel(name="ops", members=["agent://poster", "agent://member"]))
    return svc


async def test_channel_mention_reaches_non_member_inbox() -> None:
    svc = await _svc()
    await svc.send(
        Message(sender="agent://poster", to=["channel://ops"], body="hey @outsider look at this")
    )
    # Fanned into the outsider's own inbox even though it is not a channel member.
    inbox = await svc.poll("arc.agent.outsider", "agent://outsider")
    assert [m.body for m in inbox] == ["hey @outsider look at this"]
    # And still delivered to the channel stream for members.
    channel = await svc.list_channel_messages("ops")
    assert [m.body for m in channel] == ["hey @outsider look at this"]


async def test_sender_is_not_self_notified() -> None:
    svc = await _svc()
    await svc.send(
        Message(sender="agent://poster", to=["channel://ops"], body="note to @poster self")
    )
    assert await svc.poll("arc.agent.poster", "agent://poster") == []


async def test_dm_mention_does_not_fan_out() -> None:
    """An @mention inside a direct message stays private — no channel target."""
    svc = await _svc()
    await svc.send(
        Message(sender="agent://poster", to=["agent://member"], body="psst @outsider secret")
    )
    assert await svc.poll("arc.agent.outsider", "agent://outsider") == []
