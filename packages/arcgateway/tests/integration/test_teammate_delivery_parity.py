"""SPEC-065 T-944 — agent-to-agent parity on the unified delivery path (REQ-312).

SDD COMP-012 ("TeamDelivery parity") requires that a teammate message and a
human message travel the SAME delivery path, so that "injection, session
identity and audit behave identically regardless of who sent it".

Phase 2 (T-929/T-931) moved the human surface onto ``ArcAgent.deliver_message``;
the teammate surface was already there. What is NOT yet shared is everything
around that call:

  * the human path derives its session identity from the one owner of session
    keys (``arcgateway.session.build_session_key`` — SDD COMP-007), so two
    different senders get two different sessions;
  * the teammate path hard-codes ``_INBOX_SESSION = "messaging:inbox"``
    (``arcagent/modules/messaging/capabilities.py:115``), so EVERY teammate on
    the team shares one session, whatever their DID.

These tests drive both paths and COMPARE them, rather than asserting each
against a hard-coded expectation — parity is the property under test, so a test
that pins one side's current behaviour would go green on a divergence.

Both sides run their real code:

  * human: real ``SessionRouter`` -> real ``AsyncioExecutor`` -> real
    ``ArcAgent.deliver_message`` -> real arcrun loop against a gated model;
  * teammate: the real messaging module runtime, its real ``agent:ready`` hook
    (``messaging_bind_run_fn``) binding the real ``deliver_message``, and the
    real ``_handle_incoming`` — the exact callable
    ``MessagingService.subscribe`` hands every verified message to
    (``arcteam/messenger.py:662``). Signature verification happens upstream of
    that seam and is covered by the T-945/T-946 suites; what is under test here
    is what the two paths do with a message once it is accepted.

Requirements: REQ-312 (same delivery path, same injection decision, same
session identity, same audit shape; the sender never decides the run).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import _handle_incoming, messaging_bind_run_fn
from arcteam.types import Message, MsgType, Priority
from arctrust.signer import InProcessSigner

from arcgateway.executor import AsyncioExecutor, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

pytestmark = pytest.mark.asyncio

_HUMAN_ALICE = "did:arc:user:alice"
_HUMAN_BOB = "did:arc:user:bob"
_MATE_ALICE = "did:arc:testorg:agent/mate-alice"
_MATE_BOB = "did:arc:testorg:agent/mate-bob"

# The one body used on both sides, so "identical content" is literally true.
_BODY = "the quarterly numbers are ready"


# ---------------------------------------------------------------------------
# Model double — real loop, optionally parked
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 5
        self.total_tokens = 15


class _Resp:
    """Minimal end-of-turn response the real arcrun loop accepts."""

    def __init__(self) -> None:
        self.content = "ok"
        self.stop_reason = "end_turn"
        self.tool_calls: list[Any] = []
        self.cost_usd = 0.0
        self.usage = _Usage()


class _Model:
    """Answers immediately, or parks the turn while ``hold()`` is in effect.

    Parking is what makes "a run is in flight" a real condition: the arcrun loop
    is genuinely suspended, its RunHandle genuinely registered, and its
    injection queues genuinely readable.
    """

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self._gate.set()
        self.calls = 0

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    async def invoke(self, messages: Any, tools: Any = None, **_: Any) -> Any:
        self.calls += 1
        await self._gate.wait()
        return _Resp()

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Spies — wrap the real seams, never replace them
# ---------------------------------------------------------------------------


@dataclass
class _DeliveryCall:
    kwargs: dict[str, Any]
    outcome: str | None = None

    @property
    def session_key(self) -> str:
        return str(self.kwargs.get("session_key", ""))


class _DeliverySpy:
    """Records both delivery facades while preserving their real behaviour."""

    def __init__(self, inner: Callable[..., Any], stream_inner: Callable[..., Any]) -> None:
        self._inner = inner
        self._stream_inner = stream_inner
        self.calls: list[_DeliveryCall] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        call = _DeliveryCall(kwargs=dict(kwargs))
        self.calls.append(call)
        outcome = await self._inner(*args, **kwargs)
        call.outcome = outcome
        return outcome

    def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """Wrap the typed streaming facade used by the gateway transport."""
        call = _DeliveryCall(kwargs=dict(kwargs))
        self.calls.append(call)
        return self._stream(call, *args, **kwargs)

    async def _stream(self, call: _DeliveryCall, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        saw_event = False
        stream = self._stream_inner(*args, **kwargs)
        try:
            async for event in stream:
                saw_event = True
                yield event
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                await aclose()
            call.outcome = "started" if saw_event else "followed_up"

    def since(self, mark: int) -> list[_DeliveryCall]:
        return self.calls[mark:]

    def mark(self) -> int:
        return len(self.calls)


class _BusSpy:
    """Records module-bus event names and delegates to the real emit."""

    def __init__(self, inner: Callable[..., Any]) -> None:
        self._inner = inner
        self.events: list[str] = []

    async def __call__(self, event: str, data: Any = None) -> Any:
        self.events.append(event)
        return await self._inner(event, data)

    def mark(self) -> int:
        return len(self.events)

    def since(self, mark: int) -> list[str]:
        return self.events[mark:]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    agent: ArcAgent
    router: SessionRouter
    model: _Model
    delivery: _DeliverySpy
    bus: _BusSpy
    background: list[asyncio.Task[Any]] = field(default_factory=list)

    @property
    def agent_did(self) -> str:
        """The agent's REAL DID.

        Not the module constant: with ``IdentityConfig(did="")`` arctrust mints
        the DID as ``sha256(public_key)[:8]``, fresh per keypair, so a constant
        can never equal it and any assertion against one is unreachable rather
        than merely failing.
        """
        return self.agent.did

    def human_event(self, user_did: str, body: str = _BODY) -> InboundEvent:
        return InboundEvent(
            platform="telegram",
            chat_id="42",
            user_did=user_did,
            agent_did=self.agent_did,
            message=body,
        )

    def human_session(self, user_did: str) -> str:
        return build_session_key(self.agent_did, user_did)

    @staticmethod
    def teammate_message(
        sender_did: str,
        body: str = _BODY,
        *,
        priority: Priority = Priority.NORMAL,
        action_required: bool = False,
    ) -> Message:
        """A verified teammate message, exactly as ``subscribe`` would hand it over."""
        handle = sender_did.rsplit("/", 1)[-1]
        return Message(
            id=f"msg-{handle}-{priority}",
            sender=f"agent://{handle}",
            signer_did=sender_did,
            to=["agent://receiver"],
            body=body,
            msg_type=MsgType.INFO,
            priority=priority,
            action_required=action_required,
        )

    async def deliver_human(self, user_did: str, body: str = _BODY) -> None:
        await self.router.handle(self.human_event(user_did, body))
        await _settle()

    async def deliver_teammate(self, message: Message) -> None:
        await _handle_incoming(message)
        await _settle()

    async def drain(self) -> None:
        """Let every run in flight finish so the next message is a fresh turn."""
        self.model.release()
        await _wait_until(
            lambda: not self.agent._active_runs and not self.agent._delivery_stream_tasks,
            what="every active run and delivery stream to finish",
        )
        await _settle()


def _config(workspace: Path, tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="parity-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


@pytest.fixture()
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    agent = ArcAgent(config=_config(workspace, tmp_path))

    delivery = _DeliverySpy(agent.deliver_message, agent.stream_delivered_message)
    agent.deliver_message = delivery  # type: ignore[method-assign]
    agent.stream_delivered_message = delivery.stream  # type: ignore[method-assign]

    await agent.startup()
    model = _Model()
    agent._model = model

    bus = _BusSpy(agent._bus.emit)
    agent._bus.emit = bus  # type: ignore[method-assign]

    # --- teammate side: real messaging module runtime + its real ready hook ---
    _runtime.configure(
        config={"enabled": True, "entity_id": "agent://receiver", "entity_name": "receiver"},
        telemetry=None,
        workspace=workspace,
        team_root=tmp_path / "team",
        agent_name="receiver",
        identity=agent._identity,
        operator_signer=InProcessSigner(b"\x22" * 32),
    )
    await messaging_bind_run_fn(
        _ReadyCtx(
            {
                "run_fn": agent.run_collected,
                "deliver_fn": agent.deliver_message,
                "oneshot_fn": agent.run_oneshot,
                "channel_deliver_fn": None,
            }
        )
    )

    async def _factory(agent_did: str) -> ArcAgent:
        return agent

    router = SessionRouter(executor=AsyncioExecutor(agent_factory=_factory))

    h = _Harness(agent=agent, router=router, model=model, delivery=delivery, bus=bus)
    try:
        yield h
    finally:
        model.release()
        await asyncio.sleep(0.05)
        for task in h.background:
            task.cancel()
        await agent.shutdown()
        _runtime.reset()


class _ReadyCtx:
    """The ``agent:ready`` hook context shape (``ctx.data``)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


# ---------------------------------------------------------------------------
# Waiting helpers
# ---------------------------------------------------------------------------


async def _wait_until(predicate: Callable[[], bool], *, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _wait_for_run(agent: ArcAgent, session_key: str) -> Any:
    await _wait_until(
        lambda: session_key in agent._active_runs,
        what=f"a live run registered for session {session_key!r}",
    )
    return agent._active_runs[session_key]


async def _settle(seconds: float = 0.25) -> None:
    """Give the router's spawned task time to reach the agent."""
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# 1. Both senders reach the same entry point
# ---------------------------------------------------------------------------


async def test_both_senders_reach_the_same_delivery_entry_point(harness: _Harness) -> None:
    """A human and teammate message both reach the agent-owned delivery boundary.

    The floor REQ-312 stands on: one entry point, not two. If either side stops
    calling it, every other parity assertion in this file is meaningless, so
    this runs first and says so.
    """
    mark = harness.delivery.mark()
    await harness.deliver_human(_HUMAN_ALICE)
    human_calls = harness.delivery.since(mark)
    await harness.drain()

    mark = harness.delivery.mark()
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE))
    teammate_calls = harness.delivery.since(mark)
    await harness.drain()

    assert human_calls, "REQ-312: the human surface did not call the delivery facade."
    assert teammate_calls, (
        "REQ-312: the teammate surface did not call deliver_message — the two "
        "senders are not on the agent-owned delivery path."
    )


# ---------------------------------------------------------------------------
# 2. Session identity — the parity that is actually broken
# ---------------------------------------------------------------------------


async def test_two_teammates_get_distinct_sessions_exactly_as_two_humans_do(
    harness: _Harness,
) -> None:
    """Session identity must be derived the same way for both kinds of sender.

    The property, stated without naming either implementation: two DIFFERENT
    senders of the same kind get two DIFFERENT sessions. The human path gets
    this from the session-identity owner (COMP-007). The teammate path does not
    derive anything — it passes the constant ``"messaging:inbox"`` — so every
    teammate on the team lands in one shared session, and alice's conversation
    is bob's context.

    Comparing the two sides is the point: the test fails while they diverge and
    passes once the teammate path derives its key the same way, whatever that
    derivation ends up being called.
    """
    mark = harness.delivery.mark()
    await harness.deliver_human(_HUMAN_ALICE)
    await harness.drain()
    await harness.deliver_human(_HUMAN_BOB)
    await harness.drain()
    human_keys = [c.session_key for c in harness.delivery.since(mark)]

    mark = harness.delivery.mark()
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE))
    await harness.drain()
    await harness.deliver_teammate(harness.teammate_message(_MATE_BOB))
    await harness.drain()
    teammate_keys = [c.session_key for c in harness.delivery.since(mark)]

    assert len(human_keys) == 2 and len(teammate_keys) == 2, (
        f"setup: expected one delivery per message, got human={human_keys!r} "
        f"teammate={teammate_keys!r}"
    )
    assert len(set(human_keys)) == 2, (
        "baseline broken: two different humans should already get two different "
        f"sessions, got {human_keys!r}"
    )
    assert len(set(teammate_keys)) == len(set(human_keys)), (
        "REQ-312: two different teammates were delivered into "
        f"{len(set(teammate_keys))} session(s) ({teammate_keys!r}) while two "
        f"different humans get {len(set(human_keys))} ({human_keys!r}). The "
        "teammate path passes the hard-coded constant "
        "arcagent/modules/messaging/capabilities.py:115 _INBOX_SESSION instead "
        "of deriving a session key from the sender the way the human path does, "
        "so every teammate shares one session and one context."
    )


async def test_teammate_session_key_is_derived_not_hardcoded(harness: _Harness) -> None:
    """The teammate session key must come from the session-identity owner.

    Independent of the count above, and pinned to the derivation the human path
    already uses: the key for a message from ``sender`` must be the key the
    session-identity owner produces for (this agent, that sender). Today the
    teammate path never consults it.
    """
    mark = harness.delivery.mark()
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE))
    await harness.drain()
    calls = harness.delivery.since(mark)

    assert calls, "the teammate message never reached deliver_message."
    delivered_key = calls[-1].session_key
    caller_did = str(calls[-1].kwargs.get("caller_did", ""))
    expected = build_session_key(harness.agent_did, caller_did)

    assert delivered_key == expected, (
        "REQ-312 / COMP-007: the teammate path delivered into session "
        f"{delivered_key!r}, but the one owner of session identity derives "
        f"{expected!r} for (agent, {caller_did!r}). A surface that derives its "
        "own key — here a module-level constant — is a second identity for the "
        "same conversation and skips session rotation."
    )


# ---------------------------------------------------------------------------
# 3. Injection decision
# ---------------------------------------------------------------------------


async def test_teammate_message_joins_a_live_run_exactly_as_a_human_one_does(
    harness: _Harness,
) -> None:
    """With a run in flight, both kinds of sender join it rather than opening a second.

    Drives the same condition twice — a parked turn on the sender's own session,
    then a second message with identical content — and compares the two
    outcomes. A divergence here means the injection decision depends on WHO
    sent the message, which is exactly what REQ-312 forbids.
    """
    # --- human ---
    harness.model.hold()
    mark = harness.delivery.mark()
    await harness.deliver_human(_HUMAN_ALICE)
    await _wait_for_run(harness.agent, harness.human_session(_HUMAN_ALICE))
    await harness.deliver_human(_HUMAN_ALICE)
    human_handle = await _wait_for_run(harness.agent, harness.human_session(_HUMAN_ALICE))
    human_followups = human_handle.state.followup_queue.qsize()
    await harness.drain()

    # --- teammate ---
    harness.model.hold()
    mark = harness.delivery.mark()
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE))
    first = harness.delivery.since(mark)
    assert first, "the first teammate message never reached deliver_message."
    await _wait_for_run(harness.agent, first[0].session_key)
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE, body=_BODY + " (2)"))
    teammate_calls = harness.delivery.since(mark)
    teammate_outcomes = [c.outcome for c in teammate_calls]
    teammate_handle = await _wait_for_run(harness.agent, first[0].session_key)
    teammate_followups = teammate_handle.state.followup_queue.qsize()
    await harness.drain()

    assert len(harness.delivery.since(mark)) == 2, (
        "setup: the teammate path did not make one delivery call per message."
    )
    assert human_followups >= 1 and teammate_followups >= 1, (
        "REQ-312: a second message did not join the live run for both senders — "
        f"human follow-ups={human_followups}, teammate follow-ups={teammate_followups}; "
        f"teammate outcomes={teammate_outcomes!r}."
    )


async def test_the_gateway_states_no_run_preference_for_a_human_message(
    harness: _Harness,
) -> None:
    """The human surface asks for nothing: no truthy run-decision argument.

    The complement of the teammate case below. The gateway hands the message
    over and states no preference; whatever happens to it is the agent's call.
    """
    mark = harness.delivery.mark()
    await harness.deliver_human(_HUMAN_ALICE)
    calls = harness.delivery.since(mark)
    await harness.drain()

    assert calls, "the human message never reached deliver_message."
    for call in calls:
        decisions = {
            name: value
            for name, value in call.kwargs.items()
            if any(
                token in name.lower()
                for token in ("interrupt", "steer", "preempt", "abort", "cancel")
            )
        }
        assert not any(bool(value) for value in decisions.values()), (
            f"REQ-312: the gateway passed a run decision across the seam: {decisions!r}."
        )


async def test_a_teammates_interrupt_is_a_request_the_agent_must_authorize(
    harness: _Harness,
) -> None:
    """The sender's priority flag alone must never produce a mid-turn steer.

    A CRITICAL, action-required teammate message is the strongest request the
    teammate path can make — and REQ-041 makes it a *request*, honoured only on
    an explicit ALLOW from the arctrust policy pipeline. With no policy
    configured the fail-closed answer is a follow-up.

    So this removes the pipeline (a real configuration, not a stub) and asserts
    the identical message degrades to a follow-up. If the sender's own flag were
    the decision, it would steer regardless.
    """
    harness.agent._policy_pipeline = None

    harness.model.hold()
    mark = harness.delivery.mark()
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE))
    first = harness.delivery.since(mark)
    assert first, "the first teammate message never reached deliver_message."
    handle = await _wait_for_run(harness.agent, first[0].session_key)

    await harness.deliver_teammate(
        harness.teammate_message(
            _MATE_ALICE,
            body=_BODY + " (urgent)",
            priority=Priority.CRITICAL,
            action_required=True,
        )
    )
    outcomes = [c.outcome for c in harness.delivery.since(mark)]
    requested = [bool(c.kwargs.get("interrupt")) for c in harness.delivery.since(mark)]
    steers = handle.state.steer_queue.qsize()
    followups = handle.state.followup_queue.qsize()
    await harness.drain()

    assert any(requested), (
        "setup: the teammate path did not even request an interrupt for a "
        f"critical action-required message ({requested!r}), so this test is not "
        "exercising the gate it exists to prove."
    )
    assert steers == 0, (
        "REQ-312/REQ-041: a teammate's own priority flag steered the run with NO "
        "policy pipeline present. A steer needs an explicit ALLOW; absent a "
        f"pipeline the fail-closed answer is a follow-up. Outcomes: {outcomes!r}."
    )
    assert followups >= 1, (
        "REQ-302/REQ-312: the denied steer did not degrade to a follow-up on the "
        f"live run; outcomes were {outcomes!r}."
    )


# ---------------------------------------------------------------------------
# 4. Audit shape
# ---------------------------------------------------------------------------


async def test_delivery_emits_the_same_event_shape_for_both_senders(
    harness: _Harness,
) -> None:
    """One message, one turn, the same observable event shape for either sender.

    Uses the module bus — the agent's own record of what a turn did — rather
    than a hard-coded list, so the assertion is a comparison between the two
    paths and not a snapshot of today's event names. Delivery stream pumping
    schedules event observers independently of the tracked-run finalizer, so
    order is transport timing rather than sender semantics. Guarded against
    passing vacuously: the human path must have emitted something.
    """
    # Warm-up: flush any one-time startup emission so the comparison is
    # turn-for-turn rather than first-turn-vs-second.
    await harness.deliver_human(_HUMAN_ALICE)
    await harness.drain()

    mark = harness.bus.mark()
    await harness.deliver_human(_HUMAN_BOB)
    await harness.drain()
    human_events = harness.bus.since(mark)

    mark = harness.bus.mark()
    await harness.deliver_teammate(harness.teammate_message(_MATE_ALICE))
    await harness.drain()
    teammate_events = harness.bus.since(mark)

    assert human_events, "vacuous: the human turn emitted no bus events at all."
    assert Counter(teammate_events) == Counter(human_events), (
        "REQ-312: the two senders produced different turn-event shapes — "
        f"human {human_events!r} vs teammate {teammate_events!r}."
    )
