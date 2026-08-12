"""SPEC-065 T-929 / T-931 — delivery joins the live turn; background runs are safe.

The gateway must stop deciding anything about runs (SDD COMP-006). Every inbound
message is handed to the agent's delivery entry point, and the agent alone
decides whether that message joins a run already in flight, opens a new turn, or
is refused. Today ``SessionRouter._run_turn`` calls ``executor.run(event)`` for
every message, which always drives ``agent.run(...)`` — a brand new turn — and a
second message for a busy session goes into the gateway's own FIFO instead. The
agent's ``deliver_message`` entry point has no caller on any human surface.

These tests drive the REAL ``SessionRouter``, the REAL ``AsyncioExecutor`` and a
REAL ``ArcAgent`` running the REAL arcrun loop against a gated model, so a turn
can be held in flight while a second message arrives. The only instrumentation is
a spy that wraps — and still calls — the agent's own delivery API.

Requirements: REQ-302 (join the live run), REQ-303 (background runs are never
interrupted and never injection targets), REQ-312 (no run decision crosses the
gateway seam).
"""

from __future__ import annotations

import asyncio
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

from arcgateway.executor import AsyncioExecutor, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

pytestmark = pytest.mark.asyncio

_AGENT_DID = "did:arc:testorg:executor/delivery"
_USER_DID = "did:arc:user:alice"

# Argument names that would encode a run decision. The gateway may pass none of
# them with a truthy value: choosing to interrupt is the agent's call (REQ-312).
_DECISION_TOKENS = ("interrupt", "steer", "preempt", "abort", "cancel")


# ---------------------------------------------------------------------------
# Model double — real loop, parked turn
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


def _message_texts(messages: Any) -> list[str]:
    """Best-effort text of every message handed to the model (dict or object)."""
    texts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
    return texts


class _GatedModel:
    """Parks every turn inside ``invoke`` until the gate opens.

    Holding the turn open is what makes "a run is in flight" a real condition
    rather than a mocked flag: the arcrun loop is genuinely suspended, its
    RunHandle is genuinely registered, and its injection queues are genuinely
    readable.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.turns: list[list[str]] = []

    async def invoke(self, messages: Any, tools: Any = None, **_: Any) -> Any:
        self.turns.append(_message_texts(messages))
        await self.gate.wait()
        return _Resp()

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Delivery spy — wraps the real entry point, never replaces it
# ---------------------------------------------------------------------------


@dataclass
class _DeliveryCall:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    outcome: str | None = None


class _DeliverySpy:
    """Records calls to the agent's delivery API and delegates to the real one."""

    def __init__(self, inner: Callable[..., Any]) -> None:
        self._inner = inner
        self.calls: list[_DeliveryCall] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        call = _DeliveryCall(args=args, kwargs=dict(kwargs))
        self.calls.append(call)
        outcome = await self._inner(*args, **kwargs)
        call.outcome = outcome
        return outcome

    def for_session(self, session_key: str) -> list[_DeliveryCall]:
        """Calls whose arguments name ``session_key`` — however it is spelled."""
        return [c for c in self.calls if session_key in (*c.args, *c.kwargs.values())]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    agent: ArcAgent
    router: SessionRouter
    model: _GatedModel
    spy: _DeliverySpy
    session_key: str
    background: list[asyncio.Task[Any]] = field(default_factory=list)

    def inbound(self, message: str, *, user_did: str = _USER_DID) -> InboundEvent:
        return InboundEvent(
            platform="telegram",
            chat_id="42",
            user_did=user_did,
            agent_did=_AGENT_DID,
            session_key=build_session_key(_AGENT_DID, user_did),
            message=message,
        )

    def start_background_run(self, prompt: str, *, session_key: str) -> asyncio.Task[Any]:
        """Start a run through the callback the scheduler is handed on ``agent:ready``.

        ``run_collected`` is exactly what ``agent:ready`` publishes as ``run_fn``
        and what ``scheduler.execute`` invokes — so this is a real background
        run, not a test-invented one.
        """
        task: asyncio.Task[Any] = asyncio.ensure_future(
            self.agent.run_collected(prompt, session_key=session_key)
        )
        self.background.append(task)
        return task


def _config(workspace: Path, tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="delivery-agent",
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

    # Install the spy BEFORE startup: ``agent:ready`` publishes the bound
    # ``deliver_message`` as ``deliver_fn``, so a consumer that binds the
    # callback at startup is observed too.
    spy = _DeliverySpy(agent.deliver_message)
    agent.deliver_message = spy  # type: ignore[method-assign]

    await agent.startup()
    model = _GatedModel()
    agent._model = model

    async def _factory(agent_did: str) -> ArcAgent:
        return agent

    router = SessionRouter(executor=AsyncioExecutor(agent_factory=_factory))

    h = _Harness(
        agent=agent,
        router=router,
        model=model,
        spy=spy,
        session_key=build_session_key(_AGENT_DID, _USER_DID),
    )
    try:
        yield h
    finally:
        model.gate.set()
        await asyncio.sleep(0.05)
        for task in h.background:
            task.cancel()
        await agent.shutdown()


# ---------------------------------------------------------------------------
# Waiting helpers
# ---------------------------------------------------------------------------


async def _wait_until(predicate: Callable[[], bool], *, what: str, timeout: float = 3.0) -> None:
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


def _pending_injections(handle: Any) -> int:
    state = handle.state
    return int(state.steer_queue.qsize()) + int(state.followup_queue.qsize())


async def _settle(seconds: float = 0.2) -> None:
    """Give the router's spawned task time to reach the agent."""
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# T-929 — delivery joins the live turn (REQ-302)
# ---------------------------------------------------------------------------


async def test_second_message_joins_the_run_already_in_flight(harness: _Harness) -> None:
    """A second message for a busy session is delivered INTO that run.

    With an interactive run in flight, the gateway must hand the next message to
    the agent's delivery entry point. The agent then injects it into the live
    run. Two observable consequences, both asserted here:

      * the delivery API was invoked for that session, and
      * the message landed in the live run's injection queue, with the SAME
        RunHandle still registered — no second, independent run was started.
    """
    await harness.router.handle(harness.inbound("first"))
    handle = await _wait_for_run(harness.agent, harness.session_key)
    assert _pending_injections(handle) == 0

    await harness.router.handle(harness.inbound("and another thing"))
    await _settle()

    assert harness.spy.for_session(harness.session_key), (
        "REQ-302: the gateway never called the agent's delivery entry point for "
        f"session {harness.session_key!r}. A run was in flight, so this message "
        "should have joined it; instead the gateway kept the run decision for "
        "itself (SessionRouter._run_turn -> executor.run -> a fresh agent.run). "
        f"Delivery calls recorded: {harness.spy.calls!r}"
    )

    assert _pending_injections(handle) >= 1, (
        "REQ-302: the second message never reached the live run's injection "
        "queue, so it did not join the turn in flight."
    )
    assert harness.agent._active_runs.get(harness.session_key) is handle, (
        "REQ-302: a second independent run was started for a session that "
        "already had one in flight."
    )


async def test_first_message_still_starts_a_turn(harness: _Harness) -> None:
    """With nothing in flight the ordinary path must still open a turn.

    The delivery entry point reports ``"started"`` and the model really sees the
    message — routing a message through the agent must not cost us the plain
    first-message case.
    """
    await harness.router.handle(harness.inbound("hello there"))
    await _settle()

    calls = harness.spy.for_session(harness.session_key)
    assert calls, (
        "REQ-302: the gateway never called the agent's delivery entry point for "
        "the first message of an idle session; it drove a turn itself instead."
    )
    assert calls[-1].outcome == "started", (
        f"Expected the idle-session delivery to report 'started', got {calls[-1].outcome!r}."
    )

    await _wait_until(
        lambda: any("hello there" in text for turn in harness.model.turns for text in turn),
        what="the model to be invoked with the inbound message",
    )


async def test_gateway_passes_no_run_decision_across_the_seam(harness: _Harness) -> None:
    """REQ-312: the gateway decides nothing about the run.

    Two independent assertions pin the seam rather than one signature spelling:

      * structurally, no argument the gateway supplies names an interrupt/steer
        decision with a truthy value, and
      * behaviourally, the delivered message lands in the run's FOLLOW-UP queue
        and never in its STEER queue — the agent's own default with no policy
        pipeline configured. A gateway that requested an interrupt would show up
        as a steer regardless of how the argument is spelled.
    """
    await harness.router.handle(harness.inbound("first"))
    handle = await _wait_for_run(harness.agent, harness.session_key)

    await harness.router.handle(harness.inbound("second"))
    await _settle()

    calls = harness.spy.for_session(harness.session_key)
    assert calls, (
        "REQ-312 cannot be evaluated: the gateway never called the delivery "
        "entry point at all (see test_second_message_joins_the_run_already_in_flight)."
    )

    for call in calls:
        decisions = {
            name: value
            for name, value in call.kwargs.items()
            if any(token in name.lower() for token in _DECISION_TOKENS)
        }
        assert not any(bool(value) for value in decisions.values()), (
            "REQ-312: the gateway passed a run decision across the seam: "
            f"{decisions!r}. Whether to interrupt belongs to the agent."
        )

    assert handle.state.steer_queue.qsize() == 0, (
        "REQ-312: the delivered message interrupted the run mid-turn. The "
        "gateway must not cause a steer; the agent's own (fail-closed) default "
        "is a follow-up."
    )
    assert handle.state.followup_queue.qsize() >= 1, (
        "REQ-302/REQ-312: the message did not arrive as a follow-up on the live run."
    )


# ---------------------------------------------------------------------------
# T-931 — background runs are never injection targets (REQ-303)
# ---------------------------------------------------------------------------


async def test_human_message_opens_its_own_turn_beside_a_background_run(
    harness: _Harness,
) -> None:
    """Only a scheduled run in flight: the message opens a turn, the schedule runs on.

    The background run is keyed by the scheduler's own convention. The inbound
    human message must open a turn in the human's session and leave the
    scheduled run completely untouched.
    """
    harness.start_background_run("daily brief", session_key="scheduler:daily-brief")
    background = await _wait_for_run(harness.agent, "scheduler:daily-brief")

    await harness.router.handle(harness.inbound("are you there?"))
    await _settle()

    calls = harness.spy.for_session(harness.session_key)
    assert calls, (
        "REQ-303: the gateway never called the agent's delivery entry point, so "
        "the agent never got to decide that this message opens a new turn."
    )
    assert calls[-1].outcome == "started", (
        "REQ-303: with only a background run in flight the message must open a "
        f"new turn in its own session; delivery reported {calls[-1].outcome!r}."
    )

    assert _pending_injections(background) == 0, (
        "REQ-303: the human message was injected into the scheduled run."
    )
    assert not background.state.cancelled_by, (
        "REQ-303: the scheduled run was cancelled by the inbound human message."
    )


async def test_background_run_is_never_an_injection_target_under_any_key(
    harness: _Harness,
) -> None:
    """The invariant REQ-303 actually asks for, independent of the key scheme.

    Today a background run is separated from a human session only by its key
    namespace (``scheduler:<id>`` vs the 16-hex session key). That is an
    accident of naming, not a property: the moment a background run is registered
    under the session a human is talking on — a consolidation pass over the chat
    session, or any future key-scheme change — a delivery that simply injects
    into "whatever is registered" would interrupt it.

    So this drives the scheduler's own run callback onto the HUMAN's session key
    and asserts the invariant behaviourally: a background run is never an
    injection target, and the human's message is still handed to the agent.
    """
    harness.start_background_run("nightly consolidation", session_key=harness.session_key)
    background = await _wait_for_run(harness.agent, harness.session_key)

    await harness.router.handle(harness.inbound("hey, quick question"))
    await _settle()

    assert harness.spy.for_session(harness.session_key), (
        "REQ-303: the gateway never called the agent's delivery entry point."
    )

    assert background.state.steer_queue.qsize() == 0, (
        "REQ-303: a human message steered a BACKGROUND run. Background work must "
        "never be interrupted, whatever session key it happens to be registered "
        "under."
    )
    assert background.state.followup_queue.qsize() == 0, (
        "REQ-303: a human message was injected into a BACKGROUND run. A "
        "background run is not an injection target; the message must open its "
        "own turn instead."
    )
    assert not background.state.cancelled_by, (
        "REQ-303: the background run was cancelled by an inbound human message."
    )
