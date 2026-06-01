"""Entry points + RunHandle. Pure orchestration."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from arcstore.spool import request_context

from arcrun._messages import system_message, user_message
from arcrun.capabilities import CapabilityProvider, provider_tools
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import STRATEGIES, select_strategy
from arcrun.types import LoopResult, SandboxConfig

_DEFAULT_CALLER_DID = "did:arc:unknown"


def _build_state(
    capabilities: CapabilityProvider,
    system_prompt: str,
    task: str,
    *,
    messages: list[Any] | None = None,
    on_event: Callable[..., Any] | None = None,
    sandbox: SandboxConfig | None = None,
    transform_context: Callable[..., Any] | None = None,
    tool_timeout: float | None = None,
    depth: int = 0,
    max_depth: int = 3,
    tool_choice: dict[str, Any] | None = None,
    actor_did: str | None = None,
    store_raw_bodies: bool = False,
    sample_rate: float = 1.0,
) -> tuple[RunState, Sandbox]:
    """Shared setup for run() and run_async()."""
    run_id = str(uuid.uuid4())
    bus = EventBus(
        run_id=run_id,
        on_event=on_event,
        spool_actor_did=actor_did,
        store_raw_bodies=store_raw_bodies,
        sample_rate=sample_rate,
    )
    tools = provider_tools(capabilities, caller_did=actor_did or _DEFAULT_CALLER_DID)
    if not tools:
        raise ValueError("capabilities must advertise at least one capability")
    registry = ToolRegistry(tools=tools, event_bus=bus)
    sandbox_obj = Sandbox(config=sandbox, event_bus=bus)

    # When session history provided, prepend fresh system prompt.
    # System prompt is always rebuilt (never carried from old messages).
    if messages is not None:
        initial_messages = [system_message(system_prompt), *messages]
    else:
        initial_messages = [system_message(system_prompt), user_message(task)]

    state = RunState(
        messages=initial_messages,
        registry=registry,
        event_bus=bus,
        run_id=run_id,
        transform_context=transform_context,
        tool_timeout=tool_timeout,
        depth=depth,
        max_depth=max_depth,
        tool_choice=tool_choice,
    )

    return state, sandbox_obj


async def _select_and_emit(
    allowed_strategies: list[str] | None,
    model: Any,
    state: RunState,
) -> Any:
    """Select strategy, update state, emit event, return callable."""
    name = await select_strategy(allowed_strategies, model, state)
    state.strategy_name = name
    state.event_bus.emit("strategy.selected", {"strategy": name})
    return STRATEGIES[name]


async def run(
    model: Any,
    capabilities: CapabilityProvider,
    system_prompt: str,
    task: str,
    *,
    messages: list[Any] | None = None,
    max_turns: int = 25,
    allowed_strategies: list[str] | None = None,
    sandbox: SandboxConfig | None = None,
    on_event: Callable[..., Any] | None = None,
    transform_context: Callable[..., Any] | None = None,
    tool_timeout: float | None = None,
    depth: int = 0,
    max_depth: int = 3,
    tool_choice: dict[str, Any] | None = None,
    actor_did: str | None = None,
    store_raw_bodies: bool = False,
    sample_rate: float = 1.0,
) -> LoopResult:
    """Blocking entry point. Runs until task complete or max_turns."""
    state, sandbox_obj = _build_state(
        capabilities,
        system_prompt,
        task,
        messages=messages,
        on_event=on_event,
        sandbox=sandbox,
        transform_context=transform_context,
        tool_timeout=tool_timeout,
        depth=depth,
        max_depth=max_depth,
        tool_choice=tool_choice,
        actor_did=actor_did,
        store_raw_bodies=store_raw_bodies,
        sample_rate=sample_rate,
    )

    # Bind the run id as the spool correlation id so every record emitted inside
    # the run — including arcllm's llm_call, deep in the model call — inherits it.
    with request_context(state.run_id):
        strategy_fn = await _select_and_emit(allowed_strategies, model, state)
        result: LoopResult = await strategy_fn(model, state, sandbox_obj, max_turns)
    return result


async def run_async(
    model: Any,
    capabilities: CapabilityProvider,
    system_prompt: str,
    task: str,
    *,
    messages: list[Any] | None = None,
    max_turns: int = 25,
    allowed_strategies: list[str] | None = None,
    sandbox: SandboxConfig | None = None,
    on_event: Callable[..., Any] | None = None,
    transform_context: Callable[..., Any] | None = None,
    tool_timeout: float | None = None,
    depth: int = 0,
    max_depth: int = 3,
    tool_choice: dict[str, Any] | None = None,
    actor_did: str | None = None,
    store_raw_bodies: bool = False,
    sample_rate: float = 1.0,
) -> RunHandle:
    """Non-blocking entry point. Returns handle for steering."""
    state, sandbox_obj = _build_state(
        capabilities,
        system_prompt,
        task,
        messages=messages,
        on_event=on_event,
        sandbox=sandbox,
        transform_context=transform_context,
        tool_timeout=tool_timeout,
        depth=depth,
        max_depth=max_depth,
        tool_choice=tool_choice,
        actor_did=actor_did,
        store_raw_bodies=store_raw_bodies,
        sample_rate=sample_rate,
    )

    # ``create_task`` snapshots the current context, so binding the correlation
    # id here propagates it to the loop task (and any spawn it creates) even
    # though this scope exits before the task completes.
    with request_context(state.run_id):
        strategy_fn = await _select_and_emit(allowed_strategies, model, state)
        loop_task = asyncio.create_task(strategy_fn(model, state, sandbox_obj, max_turns))
    return RunHandle(state=state, task=loop_task)


class RunHandle:
    """Control interface for a running execution loop."""

    def __init__(self, state: RunState, task: asyncio.Task[LoopResult]) -> None:
        self._state = state
        self._task = task

    async def steer(self, message: str) -> None:
        """Interrupt: inject after current tool, skip remaining."""
        self._state.steer_queue.put_nowait(message)

    async def follow_up(self, message: str) -> None:
        """Queue: inject at end_turn before returning."""
        self._state.followup_queue.put_nowait(message)

    async def cancel(self) -> None:
        """Hard stop. Drains queues and sets cancel signal."""
        # Drain pending messages to prevent stale items on partial result
        for q in (self._state.steer_queue, self._state.followup_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._state.cancel_event.set()

    async def result(self) -> LoopResult:
        """Await completion. Returns final result."""
        return await self._task

    @property
    def state(self) -> RunState:
        """Read-only access to current state."""
        return self._state
