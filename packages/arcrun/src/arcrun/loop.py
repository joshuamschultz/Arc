"""Entry points + RunHandle. Pure orchestration."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from arcstore.spool import request_context

from arcrun._messages import system_message, user_message
from arcrun.capabilities import CapabilityProvider, provider_tools
from arcrun.checkpoint import LoopCheckpoint, apply_checkpoint
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import Injection, RunState
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
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
    on_checkpoint: Callable[[LoopCheckpoint], None] | None = None,
    approval_provider: Callable[..., Any] | None = None,
    approval_required_tools: frozenset[str] = frozenset(),
    max_parallel: int = 10,
    max_repeat: int | None = None,
    max_consecutive_errors: int | None = None,
    resume_from: LoopCheckpoint | None = None,
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
    # Seal the tool set for the whole run: byte-stable list keeps the provider
    # cache prefix valid and closes the mid-run tool-injection surface.
    registry.freeze()
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
        max_tokens=max_tokens,
        max_cost_usd=max_cost_usd,
        on_checkpoint=on_checkpoint,
        approval_provider=approval_provider,
        approval_required_tools=approval_required_tools,
        max_parallel=max_parallel,
        max_repeat=max_repeat,
        max_consecutive_errors=max_consecutive_errors,
    )

    # SPEC-043 REQ-003/004 — deterministic resume. The registry is rebuilt from
    # the live capabilities (fresh, frozen); apply_checkpoint verifies its tool
    # set equals the checkpoint's (fail-closed on mismatch) and restores the
    # resumable fields so the loop re-enters at the saved turn without redoing
    # completed work.
    if resume_from is not None:
        apply_checkpoint(state, resume_from)

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
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
    on_checkpoint: Callable[[LoopCheckpoint], None] | None = None,
    approval_provider: Callable[..., Any] | None = None,
    approval_required_tools: frozenset[str] = frozenset(),
    max_parallel: int = 10,
    max_repeat: int | None = None,
    max_consecutive_errors: int | None = None,
    resume_from: LoopCheckpoint | None = None,
) -> LoopResult:
    """Blocking entry point. Runs until task complete, a breaker trip, or resume."""
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
        max_tokens=max_tokens,
        max_cost_usd=max_cost_usd,
        on_checkpoint=on_checkpoint,
        approval_provider=approval_provider,
        approval_required_tools=approval_required_tools,
        max_parallel=max_parallel,
        max_repeat=max_repeat,
        max_consecutive_errors=max_consecutive_errors,
        resume_from=resume_from,
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
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
    on_checkpoint: Callable[[LoopCheckpoint], None] | None = None,
    approval_provider: Callable[..., Any] | None = None,
    approval_required_tools: frozenset[str] = frozenset(),
    max_parallel: int = 10,
    max_repeat: int | None = None,
    max_consecutive_errors: int | None = None,
    resume_from: LoopCheckpoint | None = None,
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
        max_tokens=max_tokens,
        max_cost_usd=max_cost_usd,
        on_checkpoint=on_checkpoint,
        approval_provider=approval_provider,
        approval_required_tools=approval_required_tools,
        max_parallel=max_parallel,
        max_repeat=max_repeat,
        max_consecutive_errors=max_consecutive_errors,
        resume_from=resume_from,
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

    async def steer(self, caller_did: str, message: str) -> None:
        """Interrupt: inject after current tool, skip remaining.

        ``caller_did`` must be a non-empty verified identity; arcrun records it
        but does not authorize it (the policy decision is the caller's job).
        """
        self._state.steer_queue.put_nowait(Injection.new(caller_did, message))

    async def follow_up(self, caller_did: str, message: str) -> None:
        """Queue: inject at end_turn before returning.

        ``caller_did`` must be a non-empty verified identity; arcrun records it
        but does not authorize it (the policy decision is the caller's job).
        """
        self._state.followup_queue.put_nowait(Injection.new(caller_did, message))

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
