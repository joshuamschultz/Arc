"""ReAct strategy: Reason -> Act -> Observe -> Repeat."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from arcrun._messages import TextBlock, ToolUseBlock, assistant_message, tool_result, user_message
from arcrun.executor import execute_tool_call
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.types import LoopResult


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


async def _execute_tool_calls(
    tool_calls: list[Any],
    state: RunState,
    sandbox: Sandbox,
) -> tuple[list[Any], set[str]]:
    """Execute tool calls. Tools flagged ``parallel_safe`` run concurrently.

    The loop has no knowledge of specific tool names. Concurrency is a
    declared property of the tool — any tool that opts in via
    ``Tool.parallel_safe`` is queued and dispatched in one
    ``asyncio.gather``. All other calls run sequentially in submission
    order. Returns ``(result_messages_in_order, succeeded_tool_call_ids)``;
    the set is consumed by completion-payload extraction so a tool whose
    args failed schema validation does NOT terminate the loop with
    unvalidated args (SPEC-017 R-030 — strict completion).
    """
    tool_results_map: dict[int, Any] = {}
    parallel_queue: dict[int, Any] = {}
    succeeded_ids: set[str] = set()
    steered = False

    for idx, tc in enumerate(tool_calls):
        if steered or state.cancel_event.is_set():
            tool_results_map[idx] = tool_result(tc.id, "operation cancelled: steered")
            continue

        tool = state.registry.get(tc.name)
        if tool is not None and tool.parallel_safe:
            parallel_queue[idx] = tc
        else:
            result_msg, ok = await execute_tool_call(tc, state, sandbox)
            tool_results_map[idx] = result_msg
            if ok:
                succeeded_ids.add(tc.id)

            if not state.steer_queue.empty():
                steer_msg = state.steer_queue.get_nowait()
                state.messages.append(user_message(steer_msg))
                steered = True

    # Execute queued parallel-safe calls concurrently
    if parallel_queue and not steered:
        indices = list(parallel_queue.keys())
        coros = [execute_tool_call(parallel_queue[i], state, sandbox) for i in indices]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for idx, result in zip(indices, results, strict=False):
            if isinstance(result, tuple):
                tool_results_map[idx] = result[0]
                if result[1]:
                    succeeded_ids.add(parallel_queue[idx].id)
            else:
                tc = parallel_queue[idx]
                tool_results_map[idx] = tool_result(tc.id, f"Error: {result}")

        if not state.steer_queue.empty():
            steer_msg = state.steer_queue.get_nowait()
            state.messages.append(user_message(steer_msg))
    elif parallel_queue and steered:
        for idx, tc in parallel_queue.items():
            tool_results_map[idx] = tool_result(tc.id, "operation cancelled: steered")

    # Return results in original order
    ordered = [tool_results_map[idx] for idx in sorted(tool_results_map.keys())]
    return ordered, succeeded_ids


async def react_loop(
    model: Any,
    state: RunState,
    sandbox: Sandbox,
    max_turns: int,
) -> LoopResult:
    """Run the ReAct loop until end_turn, max_turns, or cancel."""
    bus = state.event_bus
    bus.emit(
        "loop.start",
        {
            "task": state.messages[-1].content if state.messages else "",
            "tool_names": state.registry.names(),
            "strategy": "react",
        },
    )

    while state.turn_count < max_turns:
        if state.cancel_event.is_set():
            break

        # SPEC-017 R-032 — cost cap. Enforced at the top of each turn
        # so we never start a turn knowing we're already over budget.
        if state.max_cost_usd is not None and state.cost_usd >= state.max_cost_usd:
            state.completion_payload = {
                "status": "failed",
                "summary": "Cost limit reached before task completed.",
                "error": "max_cost",
            }
            bus.emit(
                "loop.completed",
                {"reason": "max_cost", "cost_usd": state.cost_usd},
            )
            return _build_result(state, None)

        bus.emit("turn.start", {"turn_number": state.turn_count + 1})

        # Check steer queue
        if not state.steer_queue.empty():
            steer_msg = state.steer_queue.get_nowait()
            state.messages.append(user_message(steer_msg))

        # Transform context hook
        messages = state.messages
        if state.transform_context is not None:
            messages = state.transform_context(messages)

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
                followup_msg = state.followup_queue.get_nowait()
                state.messages.append(user_message(followup_msg))
                _end_turn(state, bus)
                continue
            _end_turn(state, bus)
            return _build_result(state, response.content)

        # Process tool calls (parallel_safe tools dispatched concurrently)
        result_messages, succeeded_ids = await _execute_tool_calls(
            response.tool_calls, state, sandbox
        )
        state.messages.extend(result_messages)

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

    if state.turn_count >= max_turns:
        # SPEC-017 R-032 — max_turns breach synthesizes a failed
        # task_complete so consumers see a structured terminator,
        # not silent truncation.
        state.completion_payload = {
            "status": "failed",
            "summary": "Turn limit reached before task completed.",
            "error": "max_turns",
        }
        bus.emit(
            "loop.max_turns",
            {
                "turns_used": state.turn_count,
                "max_turns": max_turns,
            },
        )
        bus.emit("loop.completed", dict(state.completion_payload))

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
    """Increment turn count and emit turn.end event."""
    state.turn_count += 1
    bus.emit("turn.end", {"turn_number": state.turn_count})


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
