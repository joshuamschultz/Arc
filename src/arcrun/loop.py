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
from arcrun.strategies import STRATEGIES, _load_strategies, select_strategy
from arcrun.types import LoopResult, SandboxConfig, Tool


async def run(
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    task: str,
    *,
    max_turns: int = 25,
    allowed_strategies: list[str] | None = None,
    sandbox: SandboxConfig | None = None,
    on_event: Callable[..., Any] | None = None,
    transform_context: Callable[..., Any] | None = None,
) -> LoopResult:
    """Blocking entry point. Runs until task complete or max_turns."""
    if not tools:
        raise ValueError("tools must not be empty")

    run_id = str(uuid.uuid4())
    bus = EventBus(run_id=run_id, on_event=on_event)
    registry = ToolRegistry(tools=tools, event_bus=bus)
    sandbox_obj = Sandbox(config=sandbox, event_bus=bus)

    state = RunState(
        messages=[system_message(system_prompt), user_message(task)],
        registry=registry,
        event_bus=bus,
        run_id=run_id,
        transform_context=transform_context,
    )

    if not STRATEGIES:
        _load_strategies()

    strategy_name = await select_strategy(allowed_strategies, model, state)
    bus.emit("strategy.selected", {"strategy": strategy_name})
    strategy_fn = STRATEGIES[strategy_name]
    result: LoopResult = await strategy_fn(model, state, sandbox_obj, max_turns)
    return result


async def run_async(
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    task: str,
    **options: Any,
) -> RunHandle:
    """Non-blocking entry point. Returns handle for steering."""
    if not tools:
        raise ValueError("tools must not be empty")

    run_id = str(uuid.uuid4())
    bus = EventBus(run_id=run_id, on_event=options.get("on_event"))
    registry = ToolRegistry(tools=tools, event_bus=bus)
    sandbox_obj = Sandbox(config=options.get("sandbox"), event_bus=bus)

    state = RunState(
        messages=[system_message(system_prompt), user_message(task)],
        registry=registry,
        event_bus=bus,
        run_id=run_id,
        transform_context=options.get("transform_context"),
    )

    if not STRATEGIES:
        _load_strategies()

    max_turns = options.get("max_turns", 25)
    strategy_name = await select_strategy(options.get("allowed_strategies"), model, state)
    bus.emit("strategy.selected", {"strategy": strategy_name})
    strategy_fn = STRATEGIES[strategy_name]

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
