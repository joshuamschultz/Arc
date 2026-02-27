"""Shared tool execution pipeline. Used by all strategies."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import jsonschema

# Re-export for type reference
from arcllm.types import Message

from arcrun._messages import tool_result
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import ToolContext

_MAX_ERROR_LEN = 200


async def execute_tool_call(
    tc: Any,
    state: RunState,
    sandbox: Sandbox,
) -> tuple[Message, bool]:
    """Execute a single tool call through the full pipeline.

    Returns (tool_result_message, success).
    Strategy owns cancel/steer checks — call this only for tool calls you want to run.
    """
    bus = state.event_bus

    bus.emit("tool.start", {"name": tc.name, "arguments": tc.arguments})

    allowed, reason = await sandbox.check(tc.name, tc.arguments)
    if not allowed:
        return tool_result(tc.id, f"Error: tool denied — {reason}"), False

    tool_def = state.registry.get(tc.name)
    if tool_def is None:
        return tool_result(tc.id, f"Error: tool '{tc.name}' not found"), False

    try:
        jsonschema.validate(tc.arguments, tool_def.input_schema)
    except jsonschema.ValidationError as ve:
        return tool_result(tc.id, f"Error: invalid params — {ve.message}"), False

    ctx = ToolContext(
        run_id=state.run_id,
        tool_call_id=tc.id,
        turn_number=state.turn_count + 1,
        event_bus=bus,
        cancelled=state.cancel_event,
    )

    timeout = tool_def.timeout_seconds or state.tool_timeout
    tool_start = time.time()
    try:
        if timeout is not None:
            result = await asyncio.wait_for(tool_def.execute(tc.arguments, ctx), timeout=timeout)
        else:
            result = await tool_def.execute(tc.arguments, ctx)
    except TimeoutError:
        bus.emit("tool.error", {"name": tc.name, "error": f"timeout after {timeout}s"})
        return tool_result(tc.id, f"Error: tool timed out after {timeout}s"), False
    except Exception as exc:
        error_detail = str(exc)
        bus.emit("tool.error", {"name": tc.name, "error": error_detail})
        truncated = error_detail[:_MAX_ERROR_LEN]
        return tool_result(tc.id, f"Error: {type(exc).__name__}: {truncated}"), False

    duration_ms = (time.time() - tool_start) * 1000
    bus.emit(
        "tool.end",
        {
            "name": tc.name,
            "result_length": len(result),
            "duration_ms": duration_ms,
        },
    )

    state.tool_calls_made += 1
    return tool_result(tc.id, result), True
