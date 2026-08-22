"""Shared harness for the inbound-path race regression guards (Hermes PR #4926).

Drives the REAL ``SessionRouter``, the REAL ``AsyncioExecutor`` and a REAL
``ArcAgent`` whose model parks every turn. Parking is what makes the guard
testable: the run is genuinely in flight while the rest of a burst arrives, and
``asyncio.gather`` genuinely interleaves. An agent that answered instantly would
let each message finish before the next began, and every assertion here would
pass with the guard removed.

The only instrumentation is a spy that wraps — and still calls — the agent's own
delivery entry point, recording what the AGENT decided for each message. That is
where turns are serialised since SPEC-065, so that is where the guard is
measured.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar, Token
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

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import AsyncioExecutor
from arcgateway.session import SessionRouter

AGENT_DID = "did:arc:testorg:executor/race"

# arcrun bounds a run's injection queues; a burst larger than this cannot be
# absorbed by the live run and the surplus is refused rather than queued.
INJECTION_CAPACITY = 16


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 5
        self.total_tokens = 15


class _Resp:
    def __init__(self) -> None:
        self.content = "ok"
        self.stop_reason = "end_turn"
        self.tool_calls: list[Any] = []
        self.cost_usd = 0.0
        self.usage = _Usage()


class ParkedModel:
    """Parks every turn inside ``invoke`` until the gate opens."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    def open_gate(self) -> None:
        self.gate.set()

    async def invoke(self, messages: Any, tools: Any = None, **_: Any) -> Any:
        await self.gate.wait()
        return _Resp()

    async def close(self) -> None:
        return None


@dataclass
class _Decision:
    """What the agent decided for one delivered message."""

    session_key: str
    outcome: str | None = None
    error: BaseException | None = None


class _DeliverySpy:
    """Wrap the agent's streaming entry point and record its run decision.

    ``AsyncioExecutor`` now consumes ``DeliveryStreamSource`` rather than
    ``deliver_message``.  The streaming method is an async generator, so its
    return value no longer exposes ``"started"``/``"followed_up"``.  Observe
    the coordinator's injection lookup in the same task instead: ``None`` is
    the one message that opened the parked run, a handle is a queued message,
    and ``QueueFull`` remains the sender-visible refusal.
    """

    def __init__(self, inner: Callable[..., Any], agent: ArcAgent) -> None:
        self._inner = inner
        self._current: ContextVar[_Decision | None] = ContextVar(
            "race_delivery_decision", default=None
        )
        self.decisions: list[_Decision] = []
        coordinator = agent._run_coordinator
        original_target = coordinator.injection_target

        def observed_target(session_key: str) -> Any:
            target = original_target(session_key)
            decision = self._current.get()
            if decision is not None:
                decision.outcome = "started" if target is None else "injected"
            return target

        coordinator.injection_target = observed_target  # type: ignore[method-assign]

    def __call__(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        record = _Decision(session_key=str(kwargs.get("session_key", "")))
        self.decisions.append(record)
        return self._stream(record, self._inner(*args, **kwargs))

    async def _stream(self, record: _Decision, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        token: Token[_Decision | None] = self._current.set(record)
        try:
            async for event in stream:
                yield event
        except BaseException as exc:  # reason: record, then re-raise unchanged
            record.error = exc
            raise
        finally:
            self._current.reset(token)

    def settled(self, session_key: str) -> list[_Decision]:
        """Decisions for ``session_key`` that have actually finished."""
        return [
            d
            for d in self.decisions
            if d.session_key == session_key and (d.outcome is not None or d.error is not None)
        ]

    def started(self, session_key: str) -> int:
        return sum(1 for d in self.settled(session_key) if d.outcome == "started")

    def refused(self, session_key: str) -> int:
        return sum(1 for d in self.settled(session_key) if d.error is not None)


class RecordingAdapter:
    """Outbound channel that records everything sent back to the sender."""

    name = "telegram"
    agent_did = AGENT_DID

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, target: DeliveryTarget, message: str) -> None:
        self.sent.append(message)


@dataclass
class RaceHarness:
    agent: ArcAgent
    router: SessionRouter
    model: ParkedModel
    spy: _DeliverySpy
    adapter: RecordingAdapter
    background: list[asyncio.Task[Any]] = field(default_factory=list)

    @property
    def handles(self) -> dict[str, Any]:
        """The live run per session key, as the agent itself registered it."""
        return self.agent._active_runs

    def runs_opened(self, session_key: str) -> int:
        """How many turns this session opened — the number that must stay 1."""
        return self.spy.started(session_key)

    def refused(self, session_key: str) -> int:
        """Messages the live run had no room for; refused loudly, never silently."""
        return self.spy.refused(session_key)

    def injected(self, session_key: str) -> int:
        """Messages waiting on the live run (steer + follow-up)."""
        handle = self.agent._active_runs.get(session_key)
        if handle is None:
            return 0
        state = handle.state
        return int(state.steer_queue.qsize()) + int(state.followup_queue.qsize())

    async def settle_burst(
        self, session_key: str, n_messages: int, *, timeout: float = 5.0
    ) -> None:
        """Wait until every message of the burst has been decided.

        A delivery that never returns is itself the race bug: it means a second
        message is blocked trying to open a second turn for a session that
        already has one, and would open it the moment the first turn ended.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.spy.settled(session_key)) >= n_messages:
                return
            await asyncio.sleep(0.01)
        settled = len(self.spy.settled(session_key))
        raise AssertionError(
            f"RACE BUG: only {settled} of {n_messages} messages for session "
            f"{session_key!r} were decided within {timeout}s. The rest are blocked "
            f"waiting to open a turn this session already has — they would each "
            f"start their own run as soon as the live one ended (Hermes PR #4926)."
        )


def _config(workspace: Path, tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="race-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


async def build_harness(tmp_path: Path) -> RaceHarness:
    """Start a real agent behind a real router, with every turn parked."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    agent = ArcAgent(config=_config(workspace, tmp_path))
    spy = _DeliverySpy(agent.stream_delivered_message, agent)
    agent.stream_delivered_message = spy  # type: ignore[method-assign]  # reason: spy delegates to the real one
    await agent.startup()
    model = ParkedModel()
    agent._model = model

    async def _factory(agent_did: str) -> ArcAgent:
        return agent

    router = SessionRouter(executor=AsyncioExecutor(agent_factory=_factory))
    adapter = RecordingAdapter()
    router.register_adapter(adapter)  # type: ignore[arg-type]  # reason: structural adapter, not a subclass
    return RaceHarness(agent=agent, router=router, model=model, spy=spy, adapter=adapter)


async def teardown_harness(harness: RaceHarness) -> None:
    harness.model.open_gate()
    await asyncio.sleep(0.05)
    for task in harness.background:
        task.cancel()
    pending = tuple(harness.router._pending_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await harness.agent.shutdown()


@pytest.fixture()
async def race_harness(tmp_path: Path) -> AsyncIterator[RaceHarness]:
    harness = await build_harness(tmp_path)
    try:
        yield harness
    finally:
        await teardown_harness(harness)
