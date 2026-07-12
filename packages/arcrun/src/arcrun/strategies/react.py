"""ReAct strategy: Reason -> Act -> Observe -> Repeat."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from arcrun._messages import TextBlock, ToolUseBlock, assistant_message, tool_result, user_message
from arcrun.builtins.task_complete import BudgetBreachReason, make_budget_breach_args
from arcrun.checkpoint import to_checkpoint
from arcrun.executor import execute_tool_call
from arcrun.parallel_dispatch import BatchClassifier, dispatch_batch
from arcrun.sandbox import Sandbox
from arcrun.state import Injection, RunState
from arcrun.strategies import Strategy
from arcrun.types import LoopResult

_logger = logging.getLogger("arcrun.strategies.react")

# Longest injection preview carried in the audit event. The full message rides
# the message list as user-role data; the audit event keeps a bounded preview.
_PREVIEW_LEN = 120


def _call_signature(tc: Any) -> str:
    """Stable signature of a tool call: sha256(name + canonical args).

    Reuses the executor's canonical-JSON convention so the runaway detector
    compares *semantic* identity, not object identity (SPEC-043 REQ-020).
    """
    raw = json.dumps(
        {"name": getattr(tc, "name", ""), "args": getattr(tc, "arguments", {})},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def check_breaker(state: RunState) -> BudgetBreachReason | None:
    """Unified top-of-turn circuit breaker (SPEC-043 REQ-020..024).

    One hook point, one terminator vocabulary: token/cost/turn caps plus the
    runaway-loop and error-cascade detectors. O(1) — a handful of comparisons.
    Returns the breach reason (halt) or ``None`` (continue). Thresholds are
    ``None`` when disabled; federal supplies non-relaxable floors (REQ-024).
    """
    if state.max_cost_usd is not None and state.cost_usd >= state.max_cost_usd:
        return "max_cost"
    if state.max_tokens is not None and state.tokens_used["total"] >= state.max_tokens:
        return "max_tokens"
    if state.max_turns and state.turn_count >= state.max_turns:
        return "max_turns"
    if state.max_repeat is not None and state.runaway_count >= state.max_repeat:
        return "runaway_loop"
    if (
        state.max_consecutive_errors is not None
        and state.consecutive_tool_errors >= state.max_consecutive_errors
    ):
        return "error_cascade"
    return None


def _update_runaway(state: RunState, tool_calls: list[Any]) -> None:
    """Track the repeated-signature streak for the runaway breaker (REQ-020/025).

    A turn issuing a single distinct signature identical to the last extends the
    streak; a turn issuing *distinct* signatures (a legitimate parallel fan-out)
    counts as progress and resets it (REQ-025). No tool calls also resets.
    """
    if not tool_calls:
        state.runaway_signature = None
        state.runaway_count = 0
        return
    signatures = {_call_signature(tc) for tc in tool_calls}
    if len(signatures) != 1:
        state.runaway_signature = None
        state.runaway_count = 0
        return
    only = next(iter(signatures))
    if only == state.runaway_signature:
        state.runaway_count += 1
    else:
        state.runaway_signature = only
        state.runaway_count = 1


def _inject(state: RunState, injection: Injection, event_type: str) -> None:
    """Append an injected message as user-role data and emit its audit event.

    Single drain path for both steer and follow_up: the message is appended as
    ``user`` role (data, never system — mitigates LLM01/ASI06) and every
    injection is attributed to its ``caller_did`` in the tamper-evident event
    chain (Audit pillar). arcrun makes no trust decision here.
    """
    state.messages.append(user_message(injection.message))
    state.event_bus.emit(
        event_type,
        {
            "caller_did": injection.caller_did,
            "message_id": injection.message_id,
            "preview": injection.message[:_PREVIEW_LEN],
        },
    )


# Debug-only guard for the transform_context append-only contract. Off by
# default so the hot path pays nothing (cold start < 500ms, fleet scale).
_ASSERT_APPEND_ONLY = os.environ.get("ARCRUN_ASSERT_APPEND_ONLY") == "1"


def _check_append_only(original: list[Any], transformed: list[Any]) -> None:
    """Flag a transform_context that mutated the cached prefix.

    Contract: ``transform_context`` must be append-only between turns — a
    turn appends to a frozen prefix so the provider cache prefix stays valid.
    A deliberate compaction returns a *shorter* list (a one-time boundary
    reset), which is allowed. The anti-pattern this catches is a same-or-
    longer list whose earlier messages changed — i.e. a per-turn rewrite
    (e.g. a sliding-window prune) that silently busts the cache every turn.
    """
    if len(transformed) >= len(original) and transformed[: len(original)] != original:
        raise AssertionError(
            "transform_context mutated the cached prefix (non-append rewrite). "
            "It must append-only between turns; compaction must return a shorter list."
        )


class ReactStrategy(Strategy):
    """Wraps the existing react_loop function."""

    @property
    def name(self) -> str:
        return "react"

    @property
    def description(self) -> str:
        return (
            "Iterative tool-calling loop. Reasons about the task, calls tools, "
            "observes results, and repeats until complete. Best for multi-step "
            "problems requiring tool interaction."
        )

    @property
    def prompt_guidance(self) -> str:
        return (
            "## Execution Loop\n"
            "You operate in a Reason-Act-Observe loop. After each tool call you "
            "receive the result and decide the next action. The loop continues "
            "until you produce a final response with no tool calls.\n\n"
            "GUIDELINES:\n"
            "- Break complex work into discrete tool calls — one action per step\n"
            "- Examine tool results before deciding the next action\n"
            "- If a tool call fails, analyze the error and adapt your approach\n"
            "- After 3 failures on the same approach, try a fundamentally "
            "different method\n"
            "- When your task is complete, respond with your final answer "
            "without calling any tools"
        )

    async def __call__(
        self,
        model: Any,
        state: RunState,
        sandbox: Sandbox,
        max_turns: int,
    ) -> LoopResult:
        return await react_loop(model, state, sandbox, max_turns)


async def _resolve_approval(tc: Any, state: RunState) -> bool:
    """Proactive HITL gate — SPEC-043 REQ-010..012.

    When ``tc.name`` is flagged approval-required and a provider is injected, the
    loop SUSPENDS (an ``await``) before dispatch and asks the provider. A grant
    → proceed with the single call; ``None`` → fail closed (do not dispatch).
    arcrun mints/verifies nothing — the grant is opaque; only its presence is
    read. The provider is bound to SPEC-035 ``HumanGate`` by arcagent (REQ-012).
    """
    if state.approval_provider is None or tc.name not in state.approval_required_tools:
        return True
    state.event_bus.emit("approval.required", {"tool": tc.name, "tool_call_id": tc.id})
    grant = await state.approval_provider(tc)
    outcome = "granted" if grant is not None else "denied"
    state.event_bus.emit(f"approval.{outcome}", {"tool": tc.name, "tool_call_id": tc.id})
    return grant is not None


async def _execute_tool_calls(
    tool_calls: list[Any],
    state: RunState,
    sandbox: Sandbox,
) -> tuple[list[Any], set[str]]:
    """Execute a turn's tool calls through the ONE gated dispatch path.

    All calls flow through ``parallel_dispatch.dispatch_batch``: a ``read_only``
    batch with no shared resource runs concurrently (semaphore-bounded), anything
    state-modifying or unclassified runs sequentially (fail-closed) — one
    implementation, no ad-hoc gather (REQ-030..035). Each call still passes the
    full per-tool pipeline via ``execute_tool_call``; concurrency never skips a
    gate. Proactive HITL (REQ-010/011) resolves before dispatch: a denied call is
    not dispatched. Returns ``(result_messages_in_order, succeeded_tool_call_ids)``
    — the set gates strict completion-payload extraction (SPEC-017 R-030).
    """
    tool_results_map: dict[int, Any] = {}
    succeeded_ids: set[str] = set()

    if state.cancel_event.is_set():
        cancelled = [tool_result(tc.id, "operation cancelled: steered") for tc in tool_calls]
        return cancelled, succeeded_ids

    # Resolve proactive approvals before dispatch; a denied call is excluded.
    dispatchable: list[tuple[int, Any]] = []
    for idx, tc in enumerate(tool_calls):
        if await _resolve_approval(tc, state):
            dispatchable.append((idx, tc))
        else:
            tool_results_map[idx] = tool_result(tc.id, "operation denied: approval required")

    if dispatchable:
        calls = [tc for _, tc in dispatchable]
        results = await dispatch_batch(
            calls,
            runner=lambda tc: execute_tool_call(tc, state, sandbox),
            classifier=BatchClassifier(state.registry),
            max_parallel=state.max_parallel,
        )
        for (idx, tc), res in zip(dispatchable, results, strict=True):
            msg, ok = res
            if not isinstance(ok, bool):
                # runner raised (should not happen — execute_tool_call is total)
                tool_results_map[idx] = tool_result(tc.id, f"Error: {res[1]}")
                state.consecutive_tool_errors += 1
                continue
            tool_results_map[idx] = msg
            if ok:
                succeeded_ids.add(tc.id)
                state.consecutive_tool_errors = 0
            else:
                state.consecutive_tool_errors += 1

    ordered = [tool_results_map[idx] for idx in sorted(tool_results_map.keys())]
    return ordered, succeeded_ids


async def react_loop(
    model: Any,
    state: RunState,
    sandbox: Sandbox,
    max_turns: int,
) -> LoopResult:
    """Run the ReAct loop until end_turn, a breaker trip, or cancel."""
    bus = state.event_bus
    state.max_turns = max_turns
    bus.emit(
        "loop.start",
        {
            "task": state.messages[-1].content if state.messages else "",
            "tool_names": state.registry.names(),
            "strategy": "react",
        },
    )

    while True:
        if state.cancel_event.is_set():
            break

        # SPEC-043 REQ-020..024 — ONE top-of-turn circuit breaker folds
        # token/cost/turn caps and the runaway/error-cascade detectors into a
        # single hook + terminator vocabulary. No separate tail max_turns check.
        breach = check_breaker(state)
        if breach is not None:
            return _halt_on_breach(state, breach)

        bus.emit("turn.start", {"turn_number": state.turn_count + 1})

        # Check steer queue
        if not state.steer_queue.empty():
            _inject(state, state.steer_queue.get_nowait(), "steer.injected")

        # Transform context hook. Contract: append-only between turns (see
        # _check_append_only). Compaction is a deliberate boundary reset, not
        # a per-turn rewrite — its owner is the caller (arcagent), not arcrun.
        messages = state.messages
        if state.transform_context is not None:
            transformed = state.transform_context(messages)
            if _ASSERT_APPEND_ONLY:
                _check_append_only(messages, transformed)
            messages = transformed

        # Call model
        tools = state.registry.list_schemas()
        # Force tool calling on first turn only, then let LLM decide.
        invoke_kwargs: dict[str, Any] = {}
        if state.turn_count == 0 and state.tool_choice is not None:
            invoke_kwargs["tool_choice"] = state.tool_choice
        call_start = time.time()
        response = await model.invoke(messages, tools=tools, **invoke_kwargs)
        latency_ms = (time.time() - call_start) * 1000

        _accumulate_usage(state, response)

        bus.emit(
            "llm.call",
            {
                "model": str(type(model).__name__),
                "stop_reason": response.stop_reason,
                "tokens": state.tokens_used.copy(),
                "latency_ms": latency_ms,
                "cost_usd": getattr(response, "cost_usd", 0.0),
            },
        )

        # Build assistant message
        assistant_content: list[Any] = []
        if response.content:
            assistant_content.append(TextBlock(text=response.content))
        for tc in response.tool_calls:
            assistant_content.append(ToolUseBlock(id=tc.id, name=tc.name, arguments=tc.arguments))
        if assistant_content:
            state.messages.append(assistant_message(assistant_content))

        # End turn (no tool calls)
        if response.stop_reason == "end_turn" and not response.tool_calls:
            if not state.followup_queue.empty():
                _inject(state, state.followup_queue.get_nowait(), "followup.injected")
                _end_turn(state, bus)
                continue
            _end_turn(state, bus)
            return _build_result(state, response.content)

        # Dispatch this turn's tool calls through the one gated dispatch path.
        result_messages, succeeded_ids = await _execute_tool_calls(
            response.tool_calls, state, sandbox
        )
        state.messages.extend(result_messages)
        # Drain a steer that arrived DURING the turn only after the tool_results
        # are appended: Anthropic rejects a tool_use that is not immediately
        # followed by its tool_result, so an injected user message must never
        # land between the assistant(tool_use) and its result (the mid-run
        # interrupt an always-on agent triggers). The top-of-loop drain handles
        # a steer arriving between turns.
        if not state.steer_queue.empty():
            _inject(state, state.steer_queue.get_nowait(), "steer.injected")
        # Feed the runaway detector with THIS turn's signatures (REQ-020/025).
        _update_runaway(state, response.tool_calls)

        # SPEC-017 R-030/R-031 — any tool flagged ``signals_completion``
        # whose ``execute`` actually fired successfully (args passed
        # schema validation) terminates the loop with those validated
        # arguments. If the only completion-tool call failed validation,
        # the loop continues so the model can retry with corrected args
        # — the alternative (end the loop with unvalidated args) leaves
        # callers with a payload that doesn't match their declared
        # schema. Generic mechanism — the loop has no knowledge of
        # specific terminator tool names.
        completion = _extract_completion_payload(response.tool_calls, state.registry, succeeded_ids)
        if completion is not None:
            payload, tool_name = completion
            state.completion_payload = payload
            state.completion_tool = tool_name
            bus.emit("loop.completed", dict(payload))
            _end_turn(state, bus)
            return _build_result(state, payload.get("summary"))

        _end_turn(state, bus)

    return _build_result(state, None)


def _halt_on_breach(state: RunState, reason: BudgetBreachReason) -> LoopResult:
    """Terminate the loop on a breaker trip with a structured payload (REQ-023).

    Every breaker reason flows through the one terminator factory and emits
    ``loop.completed`` carrying the reason so consumers distinguish reasons
    without re-scanning the event chain. ``max_turns`` additionally emits the
    legacy ``loop.max_turns`` event for existing consumers (SPEC-017 R-032).
    """
    bus = state.event_bus
    state.completion_payload = make_budget_breach_args(reason=reason).model_dump(exclude_none=True)
    if reason == "max_turns":
        bus.emit("loop.max_turns", {"turns_used": state.turn_count, "max_turns": state.max_turns})
    bus.emit(
        "loop.completed",
        {"reason": reason, "cost_usd": state.cost_usd, "tokens": state.tokens_used.copy()},
    )
    return _build_result(state, None)


def _extract_completion_payload(
    tool_calls: list[Any],
    registry: Any,
    succeeded_ids: set[str],
) -> tuple[dict[str, Any], str] | None:
    """Return ``(args, tool_name)`` for the first successful completion call.

    "Successful" means the executor accepted the call's arguments (the
    JSON-schema validation in ``executor.execute_tool_call`` passed AND
    no exception was raised) — i.e. the tool's ``execute`` actually ran.
    A ``signals_completion`` tool whose args were rejected by the
    executor does NOT terminate the loop; the model gets the validation
    error as a tool result on the next turn and can retry.

    Multiple invocations are unusual — we take the first, matching
    ``response.stop_reason == "end_turn"`` semantics.
    """
    for tc in tool_calls:
        if tc.id not in succeeded_ids:
            continue
        tool = registry.get(getattr(tc, "name", ""))
        if tool is not None and tool.signals_completion:
            args = getattr(tc, "arguments", None)
            if isinstance(args, dict):
                return dict(args), tc.name
    return None


def _accumulate_usage(state: RunState, response: Any) -> None:
    """Update token counts and cost from model response."""
    usage = getattr(response, "usage", None)
    if usage:
        state.tokens_used["input"] += getattr(usage, "input_tokens", 0)
        state.tokens_used["output"] += getattr(usage, "output_tokens", 0)
        state.tokens_used["total"] += getattr(usage, "total_tokens", 0)
    state.cost_usd += getattr(response, "cost_usd", None) or 0.0


def _end_turn(state: RunState, bus: Any) -> None:
    """Increment turn count, emit turn.end, and emit a checkpoint (REQ-001/002).

    The turn boundary is the deterministic checkpoint point. When an
    ``on_checkpoint`` hook is injected the loop hands it a serializable
    :class:`LoopCheckpoint`; it never persists (that is the caller's job). With
    no hook the branch is skipped — zero hot-path overhead (AC-Sc2).
    """
    state.turn_count += 1
    bus.emit("turn.end", {"turn_number": state.turn_count})
    if state.on_checkpoint is not None:
        try:
            state.on_checkpoint(to_checkpoint(state))
        except Exception:  # reason: persistence must never break the loop
            _logger.warning("checkpoint hook raised; continuing", exc_info=True)


def _build_result(state: RunState, content: str | None) -> LoopResult:
    state.event_bus.emit(
        "loop.complete",
        {
            "content": content,
            "turns": state.turn_count,
            "tool_calls": state.tool_calls_made,
            "tokens": state.tokens_used.copy(),
            "cost": state.cost_usd,
        },
    )
    return LoopResult(
        content=content,
        turns=state.turn_count,
        tool_calls_made=state.tool_calls_made,
        tokens_used=state.tokens_used.copy(),
        strategy_used=state.strategy_name or "react",
        cost_usd=state.cost_usd,
        events=state.event_bus.events,
        completion_payload=(
            dict(state.completion_payload) if state.completion_payload is not None else None
        ),
        completion_tool=state.completion_tool,
    )
