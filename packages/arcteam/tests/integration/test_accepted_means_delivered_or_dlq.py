"""SPEC-065 T-945 — accepted means delivered or dead-lettered (REQ-313).

SDD COMP-012: "acceptance means delivered or dead-lettered with a reason".
REQ-313 states it as an absolute: WHEN the messaging layer accepts a message
THEN it SHALL either deliver it to the addressee or record it in the
dead-letter path with a reason, and SHALL NOT drop it silently.

The dead-letter path already exists — ``MessagingService._to_dlq``
(``arcteam/messenger.py:141``), readable through ``dlq_list()`` — and the
signature/replay failure modes use it. What is missing is coverage of the other
ways a message can stop travelling.

Every test here asserts on the OBSERVABLE RECORD — the addressee's stream, the
handler's received list, the DLQ — never on "no exception was raised". A
swallowed exception is precisely the failure mode REQ-313 forbids, and it
raises nothing by definition: ``_dispatch`` logs and acks a handler that
throws (``messenger.py:670``), and logs and acks a payload it cannot parse
(``messenger.py:646``). Both look like success to every caller.

Requirements: REQ-313.
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
from arcteam.messenger import STREAMS_COLLECTION, MessagingService
from arcteam.registry import EntityRegistry, UnknownHandle
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType, Message

pytestmark = pytest.mark.asyncio

DID_SENDER = "did:arc:local:agent/sender"
DID_ADDRESSEE = "did:arc:local:agent/addressee"


# ---------------------------------------------------------------------------
# Harness — one real backend, one real registry, one real messaging service
# ---------------------------------------------------------------------------


@dataclass
class _Bus:
    backend: MemoryBackend
    svc: MessagingService

    async def dlq_reasons(self) -> list[str]:
        return [str(e["meta"].get("dlq_reason", "")) for e in await self.svc.dlq_list()]

    async def stream(self, name: str) -> list[dict[str, Any]]:
        return await self.backend.read_stream(STREAMS_COLLECTION, name)


async def _bus() -> _Bus:
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
            did=DID_ADDRESSEE,
            handle="addressee",
            id="agent://addressee",
            name="Addressee",
            type=EntityType.AGENT,
        )
    )
    svc = MessagingService(
        backend, registry, audit, signer=MessageSigner(DID_SENDER, keypair.private_key)
    )
    return _Bus(backend=backend, svc=svc)


class _Collector:
    """A handler that records what it was actually given."""

    def __init__(self) -> None:
        self.received: list[Message] = []
        self.seen = asyncio.Event()

    async def __call__(self, message: Message) -> None:
        self.received.append(message)
        self.seen.set()


class _Thrower:
    """A handler whose downstream is permanently broken (not backpressure)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def __call__(self, message: Message) -> None:
        self.attempts += 1
        raise RuntimeError("downstream delivery failed")


async def _consume(svc: MessagingService, entity: str, handler: Any, *, seconds: float) -> None:
    """Run a real subscription for a bounded window, then stop it."""
    subscription = await svc.subscribe(entity, handler)
    try:
        await asyncio.sleep(seconds)
    finally:
        await subscription.stop()


# ---------------------------------------------------------------------------
# Failure mode 1 — addressee unknown
# ---------------------------------------------------------------------------


async def test_send_to_an_unknown_addressee_is_refused_or_dead_lettered() -> None:
    """A message addressed to nobody must not report success and vanish.

    ``@ghost`` is already handled: ``resolve_ref`` raises ``UnknownHandle``
    (REQ-002). The URI form is not — ``send`` never resolves it, appends the
    envelope to ``arc.agent.ghost``, and returns a message with
    ``status == "sent"``. Nothing subscribes to that stream and nothing ever
    will, so the message is neither delivered nor recorded anywhere as failed.

    Either outcome satisfies REQ-313: refuse the send (the sender learns), or
    accept and dead-letter it with a reason. Reporting success is the one thing
    that is not allowed.
    """
    bus = await _bus()
    message = Message(sender="agent://sender", to=["agent://ghost"], body="anyone home?")

    refused: Exception | None = None
    try:
        await bus.svc.send(message)
    except (UnknownHandle, ValueError) as exc:
        refused = exc

    reasons = await bus.dlq_reasons()
    assert refused is not None or reasons, (
        "REQ-313: sending to the unregistered addressee 'agent://ghost' reported "
        f"success (status={message.status!r}) and left no dead-letter record "
        f"(dlq={reasons!r}). The envelope sits in stream 'arc.agent.ghost', "
        "which no entity subscribes to — accepted, undelivered, unrecorded. "
        "The '@ghost' handle form raises UnknownHandle (REQ-002); the URI form "
        "is never resolved (arcteam/messenger.py:331-345)."
    )


# ---------------------------------------------------------------------------
# Failure mode 2 — delivery raises
# ---------------------------------------------------------------------------


async def test_a_handler_that_raises_leaves_a_dead_letter_record() -> None:
    """A permanently failing delivery must be recorded, not logged and acked.

    ``_dispatch`` catches every non-retryable handler exception, logs it, and
    acks the message (``messenger.py:670-673``). The ack is right — a poison
    message must not loop forever — but the message is then gone: undelivered,
    un-redelivered and absent from the DLQ. Nothing the operator can query says
    it ever existed.

    Asserted on the record, never on "no exception raised": a swallowed
    exception raises nothing, which is exactly why it hides.
    """
    bus = await _bus()
    thrower = _Thrower()

    await bus.svc.send(Message(sender="agent://sender", to=["agent://addressee"], body="work"))
    await _consume(bus.svc, "agent://addressee", thrower, seconds=0.4)

    reasons = await bus.dlq_reasons()
    assert thrower.attempts >= 1, "setup: the handler was never called at all."
    assert reasons, (
        "REQ-313: the delivery handler raised, the message was acked, and no "
        f"dead-letter record was written (dlq={reasons!r}). It was accepted, "
        "never delivered, and left no reason anywhere "
        "(arcteam/messenger.py:670-673)."
    )


# ---------------------------------------------------------------------------
# Failure mode 3 — malformed envelope
# ---------------------------------------------------------------------------


async def test_an_unparseable_envelope_leaves_a_dead_letter_record() -> None:
    """A poison payload must be quarantined with a reason, not just dropped.

    ``_dispatch`` validates the stream record into a ``Message`` and, on
    failure, logs and acks (``messenger.py:646-652``). Dropping it is correct;
    dropping it *silently* is not — a stream carrying payloads this consumer
    cannot parse is exactly the condition an operator needs told about.
    """
    bus = await _bus()
    collector = _Collector()

    # A real record on the addressee's stream that no ``Message`` can validate.
    await bus.backend.append_auto_seq(
        STREAMS_COLLECTION,
        "arc.agent.addressee",
        {"sender": "agent://sender", "body": None, "to": "not-a-list"},
    )

    await _consume(bus.svc, "agent://addressee", collector, seconds=0.4)

    reasons = await bus.dlq_reasons()
    assert not collector.received, "setup: the malformed record was handed to the handler."
    assert reasons, (
        "REQ-313: an unparseable envelope on the addressee's stream was acked "
        f"and discarded with no dead-letter record (dlq={reasons!r}); only a log "
        "line survives (arcteam/messenger.py:646-652)."
    )


# ---------------------------------------------------------------------------
# Failure modes already covered — regression guards
# ---------------------------------------------------------------------------


async def test_a_tampered_message_is_dead_lettered_with_a_reason() -> None:
    """The signature failure mode already records a reason. Keep it that way."""
    bus = await _bus()
    collector = _Collector()

    await bus.svc.send(Message(sender="agent://sender", to=["agent://addressee"], body="hi"))
    stored = await bus.stream("arc.agent.addressee")
    stored[0]["body"] = "tampered"

    await _consume(bus.svc, "agent://addressee", collector, seconds=0.4)

    assert not collector.received, "a tampered message reached the handler."
    assert "bad_signature" in await bus.dlq_reasons(), (
        "REQ-313 regression: a tampered message is no longer dead-lettered with a reason."
    )


async def test_a_send_the_backend_cannot_accept_is_not_reported_as_sent() -> None:
    """An unavailable backend must surface, not swallow.

    The sender is entitled to learn that its message did not land. Either the
    send raises (the sender knows) or the message is dead-lettered with a
    reason; returning normally with nothing written is the forbidden third
    option.
    """
    bus = await _bus()

    async def _refuse(*args: Any, **kwargs: Any) -> tuple[int, int]:
        raise OSError("backend unavailable")

    bus.backend.append_auto_seq = _refuse  # type: ignore[method-assign]

    raised: Exception | None = None
    try:
        await bus.svc.send(
            Message(sender="agent://sender", to=["agent://addressee"], body="lost?")
        )
    except Exception as exc:  # reason: any surfaced failure satisfies REQ-313
        raised = exc

    reasons = await bus.dlq_reasons()
    assert raised is not None or reasons, (
        "REQ-313: the backend refused the write and send() returned normally "
        f"with no dead-letter record (dlq={reasons!r}) — a silent drop."
    )


# ---------------------------------------------------------------------------
# The anti-test — no path returns success with nothing to show for it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    ["unknown_addressee", "handler_raises", "malformed_envelope", "tampered"],
)
async def test_no_accepted_message_is_both_undelivered_and_unrecorded(mode: str) -> None:
    """The invariant itself, swept over every failure mode reachable here.

    For each mode the message is accepted by the messaging layer and then
    fails. Exactly one of two observable records must exist afterwards:
    delivered to the handler, or a DLQ entry carrying a non-empty reason.
    Neither, plus a clean return, is the silent drop REQ-313 forbids.

    Deliberately assertion-on-record, not assertion-on-exception: the two modes
    this catches (``handler_raises``, ``malformed_envelope``) both swallow their
    exception, so "did it throw?" answers no in exactly the cases that are broken.
    """
    bus = await _bus()
    collector = _Collector()
    handler: Any = collector
    refused: Exception | None = None

    if mode == "unknown_addressee":
        try:
            await bus.svc.send(
                Message(sender="agent://sender", to=["agent://ghost"], body="hello?")
            )
        except (UnknownHandle, ValueError) as exc:
            refused = exc
    elif mode == "handler_raises":
        handler = _Thrower()
        await bus.svc.send(Message(sender="agent://sender", to=["agent://addressee"], body="work"))
    elif mode == "malformed_envelope":
        await bus.backend.append_auto_seq(
            STREAMS_COLLECTION,
            "arc.agent.addressee",
            {"sender": "agent://sender", "body": None, "to": "not-a-list"},
        )
    else:  # tampered
        await bus.svc.send(Message(sender="agent://sender", to=["agent://addressee"], body="hi"))
        stored = await bus.stream("arc.agent.addressee")
        stored[0]["body"] = "tampered"

    if refused is None:
        await _consume(bus.svc, "agent://addressee", handler, seconds=0.4)

    delivered = bool(collector.received)
    entries = await bus.svc.dlq_list()
    recorded = [e for e in entries if str(e["meta"].get("dlq_reason", "")).strip()]

    assert refused is not None or delivered or recorded, (
        f"REQ-313 [{mode}]: the message was accepted, was never delivered "
        "(handler received nothing), left no dead-letter entry with a reason, "
        "and nothing was raised to tell the sender. That is a silent drop — the "
        "one outcome the requirement rules out."
    )
