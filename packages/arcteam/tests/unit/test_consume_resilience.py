"""Consume loops never spin on a dead connection and never outlive a failed subscribe.

DGX 2026-10-03: a startup timeout left a half-built subscription whose consume
tasks were never stopped; closing the backend killed their NATS connection and
they retried a closed connection ~1000x/min, forever. These tests force every
interleaving with Events (no sleep races) and a recording ``_sleep``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from arctrust import generate_keypair
from arctrust.signer import InProcessSigner

from arcteam import messenger
from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.messenger import MessagingService, Subscription
from arcteam.registry import EntityRegistry
from arcteam.storage import ConsumerClosedError, Delivery, MemoryBackend
from arcteam.types import Entity, EntityType, Message

pytestmark = pytest.mark.asyncio

DID_A1 = "did:arc:local:agent/a1"
DID_A2 = "did:arc:local:agent/a2"


async def _service(backend: MemoryBackend | None = None) -> MessagingService:
    backend = backend or MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    kp = generate_keypair()
    await registry.register(
        Entity(
            did=DID_A1,
            handle="a1",
            id="agent://a1",
            name="A1",
            type=EntityType.AGENT,
            public_key=kp.public_key.hex(),
        )
    )
    await registry.register(
        Entity(
            did=DID_A2,
            handle="a2",
            id="agent://a2",
            name="A2",
            type=EntityType.AGENT,
            roles=["ops"],
        )
    )
    return MessagingService(backend, registry, audit, signer=MessageSigner(DID_A1, kp.private_key))


async def _noop(_message: Message) -> None:
    return None


class _ScriptedConsumer:
    """Consumer whose fetch replays a script of outcomes, then parks on an Event."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.exhausted = asyncio.Event()

    async def fetch(self, batch: int) -> list[Delivery]:
        self.calls += 1
        if not self.script:
            self.exhausted.set()
            await asyncio.Event().wait()  # park until cancelled
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return list(outcome)


async def _run_until_exhausted(svc: MessagingService, consumer: _ScriptedConsumer) -> None:
    task = asyncio.create_task(svc._consume_stream(consumer, _noop, set()))
    await asyncio.wait_for(consumer.exhausted.wait(), timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)
        await asyncio.sleep(0)  # yield so other tasks interleave; never a timed wait

    monkeypatch.setattr(messenger, "_sleep", fake_sleep)
    return recorded


async def test_closed_connection_ends_the_loop_without_spinning(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    svc = await _service()
    consumer = _ScriptedConsumer([ConsumerClosedError("closed")] * 50)
    caplog.set_level(logging.DEBUG)

    await asyncio.wait_for(svc._consume_stream(consumer, _noop, set()), timeout=2)

    assert consumer.calls == 1
    assert sleeps == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not any(r.exc_info for r in warnings)


async def test_transient_errors_back_off_grow_cap_and_reset(sleeps: list[float]) -> None:
    svc = await _service()
    consumer = _ScriptedConsumer([*([RuntimeError("blip")] * 12), [], RuntimeError("again")])

    await _run_until_exhausted(svc, consumer)

    grown = sleeps[:12]
    assert grown[0] == messenger._IDLE_SLEEP
    assert grown[1] == grown[0] * 2
    assert max(grown) == messenger._FETCH_BACKOFF_CAP == 30.0
    assert grown == sorted(grown)
    # sleeps[12] is the idle beat after the good (empty) fetch; the next failure
    # starts the ladder over instead of continuing at the cap.
    assert sleeps[13] == messenger._IDLE_SLEEP


async def test_one_warning_per_outage_and_info_on_recovery(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    svc = await _service()
    consumer = _ScriptedConsumer([*([RuntimeError("x")] * 7), [], *([RuntimeError("y")] * 3)])
    caplog.set_level(logging.DEBUG)

    await _run_until_exhausted(svc, consumer)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 2  # two outages: one warning each, not one per retry
    assert len(infos) == 1
    assert not any(r.exc_info for r in warnings)
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in caplog.records)


async def test_failed_subscribe_leaves_no_running_consumer(sleeps: list[float]) -> None:
    """A timeout opening the second stream must not orphan the first stream's loop."""
    backend = MemoryBackend()
    svc = await _service(backend)
    real_open = backend.open_consumer
    opened = 0

    async def flaky_open(collection: str, key: str, durable: str) -> Any:
        nonlocal opened
        opened += 1
        if opened == 2:
            raise TimeoutError("nats timeout")
        return await real_open(collection, key, durable)

    backend.open_consumer = flaky_open  # type: ignore[method-assign]  # reason: fault injection
    before = set(asyncio.all_tasks())

    with pytest.raises(TimeoutError):
        await svc.subscribe("agent://a2", _noop)

    leaked = [t for t in asyncio.all_tasks() - before if not t.done()]
    assert opened == 2
    assert leaked == []


async def test_subscription_wait_returns_when_a_consume_loop_ends_on_closed_connection(
    sleeps: list[float],
) -> None:
    svc = await _service()
    sub = Subscription("a2", _noop)
    consumer = _ScriptedConsumer([ConsumerClosedError("closed")])
    sub.track(asyncio.create_task(svc._consume_stream(consumer, _noop, set())))
    sub.track(asyncio.create_task(asyncio.Event().wait()))  # supervisor stand-in, never ends

    await asyncio.wait_for(sub.wait(), timeout=2)
    await sub.stop()

    assert all(t.done() for t in sub.tasks)


async def test_resubscribe_on_new_backend_delivers_exactly_once(sleeps: list[float]) -> None:
    """Old consumers stop before the old connection closes; the replacement resumes at the ack."""
    backend = MemoryBackend()
    svc = await _service(backend)
    got: list[str] = []
    arrived = asyncio.Event()

    async def handler(message: Message) -> None:
        got.append(message.body)
        arrived.set()

    sub = await svc.subscribe("agent://a2", handler)
    await svc.send(Message(sender="agent://a1", to=["agent://a2"], body="once"))
    await asyncio.wait_for(arrived.wait(), timeout=2)
    await sub.stop()

    # The replacement connection: same durable server state, fresh service.
    new_svc = MessagingService(backend, svc._registry, svc._audit, signer=svc._signer)
    arrived.clear()
    sub2 = await new_svc.subscribe("agent://a2", handler)
    await new_svc.send(Message(sender="agent://a1", to=["agent://a2"], body="two"))
    await asyncio.wait_for(arrived.wait(), timeout=2)
    await sub2.stop()

    assert got == ["once", "two"]
