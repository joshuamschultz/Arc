"""ReAct strategy: Reason -> Act -> Observe -> Repeat."""
from __future__ import annotations

import time
from typing import Any

import jsonschema

from arcrun._messages import TextBlock, ToolUseBlock, assistant_message, tool_result, user_message
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import LoopResult, ToolContext


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
        call_start = time.time()
        response = await model.invoke(messages, tools=tools)
        latency_ms = (time.time() - call_start) * 1000

        # Accumulate tokens/cost
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            state.tokens_used["input"] += getattr(usage, "input_tokens", 0)
            state.tokens_used["output"] += getattr(usage, "output_tokens", 0)
            state.tokens_used["total"] += getattr(usage, "total_tokens", 0)
        state.cost_usd += getattr(response, "cost_usd", 0.0)

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
            assistant_content.append(TextBlock(response.content))
        for tc in response.tool_calls:
            assistant_content.append(ToolUseBlock(tc.id, tc.name, tc.arguments))
        if assistant_content:
            state.messages.append(assistant_message(assistant_content))

        # End turn (no tool calls)
        if response.stop_reason == "end_turn" and not response.tool_calls:
            if not state.followup_queue.empty():
                followup_msg = state.followup_queue.get_nowait()
                state.messages.append(user_message(followup_msg))
                state.turn_count += 1
                bus.emit("turn.end", {"turn_number": state.turn_count})
                continue
            state.turn_count += 1
            bus.emit("turn.end", {"turn_number": state.turn_count})
            return _build_result(state, response.content)

        # Process tool calls
        tool_results: list[Any] = []
        steered = False
        for tc in response.tool_calls:
            if steered or state.cancel_event.is_set():
                tool_results.append(tool_result(tc.id, "operation cancelled: steered"))
                continue

            bus.emit("tool.start", {"name": tc.name, "arguments": tc.arguments})

            allowed, reason = await sandbox.check(tc.name, tc.arguments)
            if not allowed:
                tool_results.append(tool_result(tc.id, f"Error: tool denied — {reason}"))
                continue

            tool_def = state.registry.get(tc.name)
            if tool_def is None:
                tool_results.append(tool_result(tc.id, f"Error: tool '{tc.name}' not found"))
                continue

            try:
                jsonschema.validate(tc.arguments, tool_def.input_schema)
            except jsonschema.ValidationError as ve:
                tool_results.append(tool_result(tc.id, f"Error: invalid params — {ve.message}"))
                continue

            ctx = ToolContext(
                run_id=state.run_id,
                tool_call_id=tc.id,
                turn_number=state.turn_count + 1,
                event_bus=bus,
                cancelled=state.cancel_event,
            )
            tool_start = time.time()
            try:
                result = await tool_def.execute(tc.arguments, ctx)
            except Exception as exc:
                bus.emit("tool.error", {
                    "name": tc.name,
                    "error": str(exc),
                })
                tool_results.append(tool_result(tc.id, f"Error: {exc}"))
                continue

            duration_ms = (time.time() - tool_start) * 1000
            bus.emit("tool.end", {
                "name": tc.name,
                "result_length": len(result),
                "duration_ms": duration_ms,
            })
            state.tool_calls_made += 1
            tool_results.append(tool_result(tc.id, result))

            if not state.steer_queue.empty():
                steer_msg = state.steer_queue.get_nowait()
                state.messages.append(user_message(steer_msg))
                steered = True

        for tr in tool_results:
            state.messages.append(tr)

        state.turn_count += 1
        bus.emit("turn.end", {"turn_number": state.turn_count})

    if state.turn_count >= max_turns:
        bus.emit("loop.max_turns", {
            "turns_used": state.turn_count,
            "max_turns": max_turns,
        })

    return _build_result(state, None)


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
        strategy_used="react",
        cost_usd=state.cost_usd,
        events=state.event_bus.events,
    )
