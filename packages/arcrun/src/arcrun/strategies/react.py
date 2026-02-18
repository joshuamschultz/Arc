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

    async def __call__(self, model: Any, state: RunState, sandbox: Sandbox, max_turns: int) -> LoopResult:
        return await react_loop(model, state, sandbox, max_turns)


async def _execute_tool_calls(
    tool_calls: list[Any],
    state: RunState,
    sandbox: Sandbox,
) -> list[Any]:
    """Execute tool calls, running spawn_task calls concurrently.

    Returns tool result messages in original call order.
    """
    tool_results_map: dict[int, Any] = {}
    spawn_queue: dict[int, Any] = {}
    steered = False

    for idx, tc in enumerate(tool_calls):
        if steered or state.cancel_event.is_set():
            tool_results_map[idx] = tool_result(tc.id, "operation cancelled: steered")
            continue

        if tc.name == "spawn_task":
            spawn_queue[idx] = tc
        else:
            result_msg, _ok = await execute_tool_call(tc, state, sandbox)
            tool_results_map[idx] = result_msg

            if not state.steer_queue.empty():
                steer_msg = state.steer_queue.get_nowait()
                state.messages.append(user_message(steer_msg))
                steered = True

    # Execute queued spawn_task calls concurrently
    if spawn_queue and not steered:
        indices = list(spawn_queue.keys())
        coros = [execute_tool_call(spawn_queue[i], state, sandbox) for i in indices]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for idx, result in zip(indices, results):
            if isinstance(result, tuple):
                tool_results_map[idx] = result[0]
            else:
                tc = spawn_queue[idx]
                tool_results_map[idx] = tool_result(tc.id, f"Error: {result}")

        if not state.steer_queue.empty():
            steer_msg = state.steer_queue.get_nowait()
            state.messages.append(user_message(steer_msg))
    elif spawn_queue and steered:
        for idx, tc in spawn_queue.items():
            tool_results_map[idx] = tool_result(tc.id, "operation cancelled: steered")

    # Return results in original order
    return [tool_results_map[idx] for idx in sorted(tool_results_map.keys())]


async def react_loop(
    model: Any,
    state: RunState,
    sandbox: Sandbox,
    max_turns: int,
) -> LoopResult:
    """Run the ReAct loop until end_turn, max_turns, or cancel."""
    bus = state.event_bus
    bus.emit("loop.start", {
        "task": state.messages[-1].content if state.messages else "",
        "tool_names": state.registry.names(),
        "strategy": "react",
    })

    while state.turn_count < max_turns:
        if state.cancel_event.is_set():
            break

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

        bus.emit("llm.call", {
            "model": str(type(model).__name__),
            "stop_reason": response.stop_reason,
            "tokens": state.tokens_used.copy(),
            "latency_ms": latency_ms,
            "cost_usd": getattr(response, "cost_usd", 0.0),
        })

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

        # Process tool calls (spawn_task runs concurrently, others sequentially)
        result_messages = await _execute_tool_calls(response.tool_calls, state, sandbox)
        state.messages.extend(result_messages)

        _end_turn(state, bus)

    if state.turn_count >= max_turns:
        bus.emit("loop.max_turns", {
            "turns_used": state.turn_count,
            "max_turns": max_turns,
        })

    return _build_result(state, None)


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
    state.event_bus.emit("loop.complete", {
        "content": content,
        "turns": state.turn_count,
        "tool_calls": state.tool_calls_made,
        "tokens": state.tokens_used.copy(),
        "cost": state.cost_usd,
    })
    return LoopResult(
        content=content,
        turns=state.turn_count,
        tool_calls_made=state.tool_calls_made,
        tokens_used=state.tokens_used.copy(),
        strategy_used=state.strategy_name or "react",
        cost_usd=state.cost_usd,
        events=state.event_bus.events,
    )
