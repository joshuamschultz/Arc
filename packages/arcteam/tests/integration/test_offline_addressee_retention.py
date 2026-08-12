"""SPEC-065 T-946 — an offline addressee retains and receives (REQ-314).

REQ-314: WHERE an addressee is offline when a message is sent THEN the message
SHALL be retained and delivered when that agent next reads its mailbox, rather
than being lost with the sender's turn.

The substrate for this already exists: ``send`` appends to the addressee's
stream, and ``subscribe`` opens a durable consumer whose ack floor starts at
zero, so a first subscribe replays everything written while the addressee was
away (``arcteam/storage.py:152`` for the in-memory backend, JetStream durables
in production). These tests drive the real ``MessagingService`` over the real
``MemoryBackend`` and pin that behaviour so it cannot regress — no live broker,
no network.

They also press on the part of "next read" that is NOT obviously safe:
**exactly once**. An addressee that has been offline comes back to a backlog on
several streams at the same instant, which is precisely when the shared
``seen_ids`` dedup in ``_dispatch`` (``arcteam/messenger.py:658-672``) is at
risk — it is read, then the handler is awaited, then the id is recorded.

Requirements: REQ-314.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from arctrust import generate_keypair
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType, Message

pytestmark = pytest.mark.asyncio

DID_SENDER = "did:arc:local:agent/sender"
DID_OFFLINE = "did:arc:local:agent/offline"


@dataclass
class _Bus:
    """One shared store, one sender service, one addressee service."""

    backend: MemoryBackend
    sender: MessagingService
    addressee: MessagingService


async def _bus(*, addressee_roles: list[str] | None = None) -> _Bus:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    keypair = generate_keypair()
    await registry.register(
        Entity(
            did=DID_SENDER,
            handle="sender",
            id="agent://sender",
            name="Sender",
            type=EntityType.AGENT,
            public_key=keypair.public_key.hex(),
        )
    )
    await registry.register(
        Entity(
            did=DID_OFFLINE,
            handle="offline",
            id="agent://offline",
            name="Offline",
            type=EntityType.AGENT,
            roles=list(addressee_roles or []),
        )
    )
    sender = MessagingService(
        backend, registry, audit, signer=MessageSigner(DID_SENDER, keypair.private_key)
    )
    # The addressee runs its own service over the same store — its own process
    # in production. It never signs; verification uses the registry's keys.
    addressee = MessagingService(backend, registry, audit)
    return _Bus(backend=backend, sender=sender, addressee=addressee)


class _Reader:
    """Records every message handed to it, with a realistic awaiting body."""

    def __init__(self, *, work: float = 0.01) -> None:
        self.received: list[Message] = []
        self._work = work

    async def __call__(self, message: Message) -> None:
        # A real inbox handler awaits: it hands the message to the agent's
        # delivery entry point, which opens or joins a turn. Awaiting here is
        # what makes the dedup window in ``_dispatch`` reachable — an instant
        # handler never yields and would hide the race behind luck.
        await asyncio.sleep(self._work)
        self.received.append(message)


async def _read_inbox(
    svc: MessagingService, entity: str, reader: _Reader, *, seconds: float
) -> None:
    """The addressee comes online, reads its mailbox for a while, goes away again."""
    subscription = await svc.subscribe(entity, reader)
    try:
        await asyncio.sleep(seconds)
    finally:
        await subscription.stop()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def test_a_message_sent_to_an_offline_agent_arrives_on_its_next_read() -> None:
    """Nobody is listening when the message is sent; it must still arrive later."""
    bus = await _bus()

    # No subscription exists at this moment — the addressee is offline.
    await bus.sender.send(
        Message(sender="agent://sender", to=["agent://offline"], body="while you were out")
    )

    reader = _Reader()
    await _read_inbox(bus.addressee, "agent://offline", reader, seconds=0.4)

    assert [m.body for m in reader.received] == ["while you were out"], (
        "REQ-314: a message sent while the addressee was offline did not reach "
        f"it on its next read; it received {[m.body for m in reader.received]!r}."
    )


async def test_retention_outlives_the_senders_turn() -> None:
    """The sender is gone before the addressee ever looks. The message survives.

    REQ-314's exact wording — "rather than being lost with the sender's turn".
    The sender's service object is dropped after the send, so nothing it holds
    can be what keeps the message alive.
    """
    bus = await _bus()
    await bus.sender.send(
        Message(sender="agent://sender", to=["agent://offline"], body="outlives the sender")
    )
    bus.sender = None  # type: ignore[assignment]  # the sender's turn is over

    reader = _Reader()
    await _read_inbox(bus.addressee, "agent://offline", reader, seconds=0.4)

    assert [m.body for m in reader.received] == ["outlives the sender"], (
        "REQ-314: the retained message did not survive the end of the sender's "
        f"turn; the addressee received {[m.body for m in reader.received]!r}."
    )


async def test_a_backlog_is_replayed_in_order_on_the_next_read() -> None:
    """Several messages accumulated while offline all arrive, oldest first."""
    bus = await _bus()
    for n in range(3):
        await bus.sender.send(
            Message(sender="agent://sender", to=["agent://offline"], body=f"msg-{n}")
        )

    reader = _Reader()
    await _read_inbox(bus.addressee, "agent://offline", reader, seconds=0.6)

    assert [m.body for m in reader.received] == ["msg-0", "msg-1", "msg-2"], (
        "REQ-314: the offline backlog was not replayed in full and in order; got "
        f"{[m.body for m in reader.received]!r}."
    )


# ---------------------------------------------------------------------------
# Exactly once
# ---------------------------------------------------------------------------


async def test_a_retained_message_is_not_redelivered_after_it_is_read() -> None:
    """Read once, then come back: the mailbox must not replay what was acked.

    The other half of "delivered when that agent next reads" — retained until
    read, and not one delivery more. A durable that forgot its ack floor would
    hand the same message over on every restart, forever.
    """
    bus = await _bus()
    await bus.sender.send(
        Message(sender="agent://sender", to=["agent://offline"], body="read me once")
    )

    first = _Reader()
    await _read_inbox(bus.addressee, "agent://offline", first, seconds=0.4)
    second = _Reader()
    await _read_inbox(bus.addressee, "agent://offline", second, seconds=0.4)

    assert len(first.received) == 1, (
        f"setup: the first read got {len(first.received)} copies, expected 1."
    )
    assert second.received == [], (
        "REQ-314: the message was redelivered on the next read after it had "
        f"already been read and acked; got {[m.body for m in second.received]!r}."
    )


async def test_a_backlog_fanned_to_two_streams_is_delivered_exactly_once() -> None:
    """One message, two of the addressee's streams, one delivery.

    An entity subscribes to its inbox and to one stream per role. A message
    addressed to both lands in both — deliberately, so the addressee is woken
    whichever it watches — and ``_dispatch`` is meant to collapse the duplicate
    via the ``seen_ids`` set it shares across the subscription's consume loops.

    Being offline is what makes this hard: both copies are already waiting, so
    both loops fetch them in the same instant and race through the window
    between reading ``seen_ids`` and adding to it, either side of
    ``await handler(message)`` (``arcteam/messenger.py:658-672``). Live traffic
    hides this — the copies arrive milliseconds apart — which is exactly why the
    offline case is the one REQ-314 has to state.
    """
    bus = await _bus(addressee_roles=["ops"])

    await bus.sender.send(
        Message(
            sender="agent://sender",
            to=["agent://offline", "role://ops"],
            body="one message, two streams",
        )
    )

    reader = _Reader()
    await _read_inbox(bus.addressee, "agent://offline", reader, seconds=0.5)

    bodies = [m.body for m in reader.received]
    assert bodies, "REQ-314: the fanned-out message never arrived at all."
    assert len(bodies) == 1, (
        "REQ-314: an offline addressee's backlog delivered the same message "
        f"{len(bodies)} times ({bodies!r}). The two consume loops both read "
        "seen_ids before either recorded the id — the dedup check and the "
        "record sit either side of an await."
    )


# ---------------------------------------------------------------------------
# The pull path
# ---------------------------------------------------------------------------


async def test_the_pull_path_also_returns_what_was_retained() -> None:
    """``receive`` — the path the check-inbox tool drives — sees the backlog too.

    Retention must not depend on which read the agent performs. ``receive``
    verifies each message and returns it; the cursor advances only on ``ack``.
    """
    bus = await _bus()
    await bus.sender.send(Message(sender="agent://sender", to=["agent://offline"], body="pull me"))

    got: list[Any] = await bus.addressee.receive("arc.agent.offline", "agent://offline")

    assert [m.body for m in got] == ["pull me"], (
        "REQ-314: the pull path did not return the message retained while the "
        f"addressee was offline; got {[m.body for m in got]!r}."
    )
