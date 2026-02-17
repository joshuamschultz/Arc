"""Entry points + RunHandle. Pure orchestration."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

from arcrun._messages import system_message, user_message
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import STRATEGIES, select_strategy
from arcrun.types import LoopResult, SandboxConfig, Tool


def _build_state(
    tools: list[Tool],
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
) -> tuple[RunState, Sandbox]:
    """Shared setup for run() and run_async()."""
    if not tools:
        raise ValueError("tools must not be empty")

    run_id = str(uuid.uuid4())
    bus = EventBus(run_id=run_id, on_event=on_event)
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
    )

    return state, sandbox_obj


def _inject_spawn_tool(
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    state: RunState,
    sandbox: SandboxConfig | None,
    allowed_strategies: list[str] | None,
) -> None:
    """Inject spawn_task tool if recursion depth allows."""
    if state.depth < state.max_depth:
        from arcrun.builtins.spawn import make_spawn_tool

        spawn_tool = make_spawn_tool(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            state=state,
            sandbox=sandbox,
            allowed_strategies=allowed_strategies,
        )
        state.registry.add(spawn_tool)


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
    tools: list[Tool],
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
) -> LoopResult:
    """Blocking entry point. Runs until task complete or max_turns."""
    state, sandbox_obj = _build_state(
        tools, system_prompt, task,
        messages=messages,
        on_event=on_event, sandbox=sandbox,
        transform_context=transform_context, tool_timeout=tool_timeout,
        depth=depth, max_depth=max_depth,
    )

    _inject_spawn_tool(model, tools, system_prompt, state, sandbox, allowed_strategies)

    strategy_fn = await _select_and_emit(allowed_strategies, model, state)
    return await strategy_fn(model, state, sandbox_obj, max_turns)


async def run_async(
    model: Any,
    tools: list[Tool],
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
) -> RunHandle:
    """Non-blocking entry point. Returns handle for steering."""
    state, sandbox_obj = _build_state(
        tools, system_prompt, task,
        messages=messages,
        on_event=on_event, sandbox=sandbox,
        transform_context=transform_context, tool_timeout=tool_timeout,
        depth=depth, max_depth=max_depth,
    )

    _inject_spawn_tool(model, tools, system_prompt, state, sandbox, allowed_strategies)

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
        """Hard stop. Sets cancel signal. Returns partial result."""
        self._state.cancel_event.set()

    async def result(self) -> LoopResult:
        """Await completion. Returns final result."""
        return await self._task

    @property
    def state(self) -> RunState:
        """Read-only access to current state."""
        return self._state
