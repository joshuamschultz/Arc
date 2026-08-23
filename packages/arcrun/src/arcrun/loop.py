"""Entry points + RunHandle. Pure orchestration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcstore.spool import request_context

from arcrun._messages import ContentBlock, SystemPrompt, system_messages, user_message
from arcrun.capabilities import CapabilityProvider, provider_tools
from arcrun.checkpoint import LoopCheckpoint, apply_checkpoint
from arcrun.dynamic.seal import RunSeal
from arcrun.events import EventBus
from arcrun.ledger import ToolExecutionLedger
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import Injection, RunDeadlineExceededError, RunState, RunWorkCancelledError
from arcrun.strategies import STRATEGIES, available_strategies, select_strategy
from arcrun.types import LoopResult, SandboxConfig

_logger = logging.getLogger(__name__)

_DEFAULT_CALLER_DID = "did:arc:unknown"


def _build_state(
    capabilities: CapabilityProvider,
    system_prompt: SystemPrompt,
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
    run_id: str | None = None,
    work_dir: Path | None = None,
    seal: RunSeal | None = None,
    stream_event: Callable[[str, dict[str, Any]], None] | None = None,
    deadline: float | None = None,
    tool_ledger: ToolExecutionLedger | None = None,
) -> tuple[RunState, Sandbox]:
    """Shared setup for run() and run_async()."""
    # A caller (e.g. the task dispatcher) may pin the run id so it can link the
    # run to durable state before the loop starts; otherwise one is minted here.
    run_id = run_id or str(uuid.uuid4())
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
        initial_messages = [*system_messages(system_prompt), *messages]
    else:
        initial_messages = [*system_messages(system_prompt), user_message(task)]

    state = RunState(
        messages=initial_messages,
        registry=registry,
        event_bus=bus,
        run_id=run_id,
        work_dir=work_dir,
        seal=seal,
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
        stream_event=stream_event,
        deadline=deadline,
        tool_ledger=tool_ledger,
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
    system_prompt: SystemPrompt,
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
    run_id: str | None = None,
    work_dir: Path | None = None,
    seal: RunSeal | None = None,
    on_handle: Callable[[RunHandle], None] | None = None,
    stream_event: Callable[[str, dict[str, Any]], None] | None = None,
    deadline: float | None = None,
    tool_ledger: ToolExecutionLedger | None = None,
) -> LoopResult:
    """Blocking entry point. Runs until task complete, a breaker trip, or resume.

    ``on_handle``, when set, is called once with the live :class:`RunHandle`
    before the result is awaited — the seam a streaming caller uses to expose the
    handle to an operator kill-switch (GAP-A) without giving up the blocking
    return contract. Inert when None.
    """
    handle = await run_async(
        model,
        capabilities,
        system_prompt,
        task,
        messages=messages,
        max_turns=max_turns,
        allowed_strategies=allowed_strategies,
        sandbox=sandbox,
        on_event=on_event,
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
        run_id=run_id,
        work_dir=work_dir,
        seal=seal,
        stream_event=stream_event,
        deadline=deadline,
        tool_ledger=tool_ledger,
    )
    if on_handle is not None:
        on_handle(handle)
    return await handle.result()


async def run_oneshot(
    model: Any,
    *,
    user: str,
    system: str = "",
    max_tokens: int | None = 8,
    timeout: float | None = None,
    on_event: Callable[..., Any] | None = None,
    actor_did: str | None = None,
    run_id: str | None = None,
) -> LoopResult:
    """One bounded model call — the entry for a decision not worth a run.

    Gates, labels, summaries and tiebreaks need a model without needing a loop.
    This is where that need is served, so no layer above arcrun has a reason to
    hold a provider handle of its own (ADR-032).

    The cost is bounded before the call is made: one turn, no tools, an output
    ceiling, and — when ``timeout`` is set — a deadline, so a hung provider
    raises ``TimeoutError`` instead of blocking its caller indefinitely. A
    caller whose answer is prose rather than a verdict passes ``max_tokens=None``
    and says so; the ceiling is never removed silently. Usage and cost come back
    on the result, because an unmetered call is spend nobody can see (LLM10).

    ``system`` is optional: an empty one sends the user turn alone.
    """
    run_id = run_id or str(uuid.uuid4())
    bus = EventBus(run_id=run_id, on_event=on_event, spool_actor_did=actor_did)
    prelude = list(system_messages(system)) if system else []
    state = RunState(
        messages=[*prelude, user_message(user)],
        registry=ToolRegistry(tools=[], event_bus=bus),
        event_bus=bus,
        run_id=run_id,
        max_tokens=max_tokens,
        strategy_name="oneshot",
    )
    call = available_strategies()["oneshot"](model, state, Sandbox(config=None, event_bus=bus), 1)
    if timeout is None:
        return await call
    return await asyncio.wait_for(call, timeout=timeout)


class StructuredCallError(RuntimeError):
    """A forced structured call returned no tool call to read."""


async def run_structured(
    model: Any,
    messages: list[Any],
    *,
    tool: Any,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One forced tool call; return the model's tool arguments.

    The structured-output sibling of :func:`run_oneshot`. A caller that needs a
    schema filled — a plan DAG, a labelled extraction — forces exactly ``tool``
    and reads its arguments back, without holding a provider handle or running a
    loop (ADR-032). Every model call in the stack keeps one owner: arcrun.

    Bounded like ``run_oneshot``: one turn, one tool, an optional output ceiling
    and deadline. ``timeout`` raises :class:`TimeoutError` rather than blocking a
    caller on a hung provider. Raises :class:`StructuredCallError` when the model
    answers without calling the tool, so a caller never mistakes an empty draft
    for a valid one. The call is spooled under the ambient run context already in
    scope, so it lands in the caller's trace.
    """
    cap: dict[str, Any] = {"max_tokens": max_tokens} if max_tokens is not None else {}
    call = model.invoke(
        messages,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool.name},
        **cap,
    )
    response = await (asyncio.wait_for(call, timeout=timeout) if timeout is not None else call)
    calls = getattr(response, "tool_calls", None) or []
    if not calls:
        raise StructuredCallError(f"model emitted no '{tool.name}' tool call")
    return dict(calls[0].arguments)


async def run_async(
    model: Any,
    capabilities: CapabilityProvider,
    system_prompt: SystemPrompt,
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
    run_id: str | None = None,
    work_dir: Path | None = None,
    seal: RunSeal | None = None,
    stream_event: Callable[[str, dict[str, Any]], None] | None = None,
    deadline: float | None = None,
    tool_ledger: ToolExecutionLedger | None = None,
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
        run_id=run_id,
        work_dir=work_dir,
        seal=seal,
        stream_event=stream_event,
        deadline=deadline,
        tool_ledger=tool_ledger,
    )

    # ``create_task`` snapshots the current context, so binding the correlation
    # id here propagates it to the loop task (and any spawn it creates) even
    # though this scope exits before the task completes.
    # Strategy selection happens INSIDE the task, not before it. Choosing a
    # strategy can cost a model call, and until the task exists there is no
    # RunHandle — so an operator cancel or a teammate's interrupt arriving
    # during that call would have nothing to reach (ASI09/ASI10). Creating the
    # task first makes the run steerable from the moment it is started.
    with request_context(state.run_id):
        loop_task = asyncio.create_task(
            _select_then_run(allowed_strategies, model, state, sandbox_obj, max_turns)
        )
    return RunHandle(state=state, task=loop_task)


async def _select_then_run(
    allowed_strategies: list[str] | None,
    model: Any,
    state: RunState,
    sandbox_obj: Sandbox,
    max_turns: int,
) -> LoopResult:
    """Pick the strategy, then run it, both within the already-live task.

    ROBUSTNESS INVARIANT: every run terminates. A strategy that raises — a model
    call that errors after retries, a context-assembly failure, a tool-dispatch
    bug — must still emit the universal ``loop.complete`` terminal. Without this
    the run never gets an end-of-run marker and dangles forever ("running" then
    "stale"): the single largest reason runs did not finish. The terminal here
    carries an ``error`` field so the run reads as failed, not silently ok.
    """
    try:
        strategy_fn = await _select_and_emit(allowed_strategies, model, state)
        result: LoopResult = await strategy_fn(model, state, sandbox_obj, max_turns)
        return result
    except RunWorkCancelledError:
        from arcrun.strategies.react import _halt_on_cancel

        return _halt_on_cancel(state)
    except RunDeadlineExceededError:
        from arcrun.strategies.react import _halt_on_breach

        return _halt_on_breach(state, "deadline")
    except BaseException as exc:
        # A strategy that unwinds early — a model error after retries, a security
        # refusal (SealBroken), a cancel, a bug — otherwise leaves NO terminal and
        # the run dangles forever ("running" -> "stale"), the dominant reason runs
        # did not finish. Emit the universal terminal here IF the strategy did not,
        # then RE-RAISE so the exception still reaches the caller and security
        # refusals still refuse. The terminal fires exactly once either way.
        if any(e.type == "loop.complete" for e in state.event_bus.events):
            raise
        _logger.warning(
            "strategy unwound without a terminal (%s); emitting one so the run finishes",
            type(exc).__name__,
        )
        state.event_bus.emit(
            "loop.complete",
            {
                "content": None,
                "turns": state.turn_count,
                "tool_calls": state.tool_calls_made,
                "tokens": dict(state.tokens_used),
                "cost": state.cost_usd,
                "error": type(exc).__name__,
                "error_message": str(exc)[:300],
            },
        )
        raise


class RunHandle:
    """Control interface for a running execution loop."""

    def __init__(self, state: RunState, task: asyncio.Task[LoopResult]) -> None:
        self._state = state
        self._task = task

    async def steer(self, caller_did: str, message: str | list[ContentBlock]) -> None:
        """Interrupt: inject after current tool, skip remaining.

        ``caller_did`` must be a non-empty verified identity; arcrun records it
        but does not authorize it (the policy decision is the caller's job).
        ``message`` is blocks when the sender injected more than words.
        """
        self._state.steer_queue.put_nowait(Injection.new(caller_did, message))

    async def follow_up(self, caller_did: str, message: str | list[ContentBlock]) -> None:
        """Queue: inject at end_turn before returning.

        ``caller_did`` must be a non-empty verified identity; arcrun records it
        but does not authorize it (the policy decision is the caller's job).
        ``message`` is blocks when the sender injected more than words.
        """
        self._state.followup_queue.put_nowait(Injection.new(caller_did, message))

    async def cancel(self, caller_did: str, reason: str | None = None) -> None:
        """Hard stop, attributed to a verified caller. Drains queues, sets signal.

        ``caller_did`` must be a non-empty verified identity: arcrun records it so
        the kill switch is attributable (ASI09/ASI10) but does not authorize it —
        that policy decision belongs to the caller, mirroring ``steer``
        and ``follow_up``. ``reason`` is an optional operator note carried into the
        structured cancelled result and the ``loop.cancelled`` audit event.
        """
        if not caller_did:
            raise ValueError("caller_did is required to cancel a run")
        self._state.cancelled_by = caller_did
        self._state.cancel_reason = reason or ""
        # Drain pending messages to prevent stale items on partial result
        for q in (self._state.steer_queue, self._state.followup_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._state.cancel_event.set()
        await self._state.cancel_active_work()

    async def result(self) -> LoopResult:
        """Await completion. Returns final result."""
        return await self._task

    @property
    def state(self) -> RunState:
        """Read-only access to current state."""
        return self._state
