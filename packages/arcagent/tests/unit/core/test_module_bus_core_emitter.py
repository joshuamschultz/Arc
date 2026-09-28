"""SPEC-083 — the module bus certifies events the agent core emitted.

Every module receives the same :class:`ModuleBus`, so a plain ``emit`` proves
nothing about who sent an event. The agent claims the bus's one-shot core emitter
before any module is configured; only events sent through it are marked
``core_certified``. A second claim (a module trying to obtain the capability) is
refused.
"""

from __future__ import annotations

import pytest

from arcagent.core.module_bus import EventContext, ModuleBus


async def _seen(bus: ModuleBus, event: str) -> list[EventContext]:
    seen: list[EventContext] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx)

    bus.subscribe(event, handler, module_name="probe")
    return seen


async def test_plain_emit_is_not_core_certified() -> None:
    bus = ModuleBus()
    seen = await _seen(bus, "knowledge:shared_attached")

    await bus.emit("knowledge:shared_attached", {}, agent_did="did:arc:a")

    assert [ctx.core_certified for ctx in seen] == [False]


async def test_core_emitter_marks_the_event_certified() -> None:
    bus = ModuleBus()
    core = bus.claim_core_emitter()
    seen = await _seen(bus, "knowledge:shared_attached")

    ctx = await core.emit("knowledge:shared_attached", {"k": 1}, agent_did="did:arc:a")

    assert ctx.core_certified is True
    assert [(c.core_certified, c.agent_did, c.data) for c in seen] == [
        (True, "did:arc:a", {"k": 1})
    ]


def test_core_emitter_can_be_claimed_only_once() -> None:
    bus = ModuleBus()
    bus.claim_core_emitter()

    with pytest.raises(RuntimeError, match="already claimed"):
        bus.claim_core_emitter()


async def test_a_handler_cannot_certify_a_forged_event_for_later_handlers() -> None:
    bus = ModuleBus()
    later: list[bool] = []

    async def forger(ctx: EventContext) -> None:
        with pytest.raises(AttributeError):
            ctx.core_certified = True  # type: ignore[misc]  # reason: proving it is read-only

    async def victim(ctx: EventContext) -> None:
        later.append(ctx.core_certified)

    bus.subscribe("knowledge:shared_attached", forger, priority=10, module_name="forger")
    bus.subscribe("knowledge:shared_attached", victim, priority=100, module_name="victim")

    await bus.emit("knowledge:shared_attached", {}, agent_did="did:arc:a")

    assert later == [False]
