"""Shared tool execution pipeline. Used by all strategies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import arcllm
import jsonschema

from arcrun._messages import tool_result
from arcrun.ledger import (
    CanonicalToolArgumentsError,
    ToolExecutionIntent,
    ToolExecutionOutcome,
    tool_invocation_key,
)
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import ParentRunContext, ToolContext

_MAX_ERROR_LEN = 200


def _digest_and_size(value: Any) -> tuple[str | None, int | None]:
    """sha256 + byte length of a tool's args/result CONTENT (SPEC-028 C1).

    Computed here, where ``tc.arguments`` (a dict) and ``result`` (a str) both
    exist — never downstream from a length, which would digest the wrong thing.
    Strings hash as their UTF-8 bytes; structured args hash as canonical JSON.
    """
    if value is None:
        return None, None
    raw = (
        value.encode("utf-8")
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    )
    return hashlib.sha256(raw).hexdigest(), len(raw)


async def execute_tool_call(
    tc: Any,
    state: RunState,
    sandbox: Sandbox,
) -> tuple[arcllm.Message, bool]:
    """Execute a single tool call through the full pipeline.

    Returns (tool_result_message, success).
    Strategy owns cancel/steer checks — call this only for tool calls you want to run.
    """
    bus = state.event_bus

    # Digest the args once at source (C1) and reuse for start + error events.
    args_digest, args_size = _digest_and_size(tc.arguments)
    bus.emit(
        "tool.start",
        {
            "name": tc.name,
            "arguments": tc.arguments,
            "args_digest": args_digest,
            "args_size": args_size,
        },
    )

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

    invocation_key: str | None = None
    if state.tool_ledger is not None:
        try:
            invocation_key = tool_invocation_key(state.run_id, tc.id, tc.name, tc.arguments)
        except CanonicalToolArgumentsError as exc:
            bus.emit("tool.error", {"name": tc.name, "error": type(exc).__name__})
            return tool_result(tc.id, "Error: tool arguments are not canonical JSON"), False
        intent = ToolExecutionIntent(
            invocation_key=invocation_key,
            run_id=state.run_id,
            tool_call_id=tc.id,
            tool_name=tc.name,
            arguments_digest=args_digest or "",
        )
        try:
            entry = await state.await_work(state.tool_ledger.begin(intent))
        except Exception as exc:
            message = f"Error: tool ledger unavailable: {type(exc).__name__}"
            bus.emit("tool.error", {"name": tc.name, "error": type(exc).__name__})
            return tool_result(tc.id, message), False
        if entry.status == "completed" and entry.outcome is not None:
            bus.emit("tool.replayed", {"name": tc.name, "invocation_key": invocation_key})
            bus.emit("tool.end", {"name": tc.name, "replayed": True})
            state.tool_calls_made += 1
            return tool_result(tc.id, entry.outcome.content), entry.outcome.success
        if entry.status != "new":
            bus.emit(
                "tool.reconciliation_required", {"name": tc.name, "invocation_key": invocation_key}
            )
            bus.emit("tool.error", {"name": tc.name, "error": "reconciliation_required"})
            return tool_result(tc.id, "Error: prior tool intent requires reconciliation"), False

    ctx = ToolContext(
        run_id=state.run_id,
        tool_call_id=tc.id,
        turn_number=state.turn_count + 1,
        event_bus=bus,
        cancelled=state.cancel_event,
        parent_state=ParentRunContext(
            run_id=state.run_id,
            depth=state.depth,
            max_depth=state.max_depth,
            event_bus=state.event_bus,
            tokens_used=state.tokens_used.copy(),
            cost_usd=state.cost_usd,
            tool_calls_made=state.tool_calls_made,
        ),
    )

    timeout = tool_def.timeout_seconds or state.tool_timeout
    tool_start = time.time()
    try:
        if timeout is not None:
            result = await asyncio.wait_for(
                state.await_work(tool_def.execute(tc.arguments, ctx)), timeout=timeout
            )
        else:
            result = await state.await_work(tool_def.execute(tc.arguments, ctx))
    except TimeoutError:
        bus.emit(
            "tool.error",
            {
                "name": tc.name,
                "error": f"timeout after {timeout}s",
                "args_digest": args_digest,
                "args_size": args_size,
            },
        )
        return tool_result(tc.id, f"Error: tool timed out after {timeout}s"), False
    except Exception as exc:  # reason: best-effort — record + continue
        error_detail = str(exc)
        bus.emit(
            "tool.error",
            {
                "name": tc.name,
                "error": error_detail,
                "args_digest": args_digest,
                "args_size": args_size,
            },
        )
        truncated = error_detail[:_MAX_ERROR_LEN]
        return tool_result(tc.id, f"Error: {type(exc).__name__}: {truncated}"), False

    duration_ms = (time.time() - tool_start) * 1000
    if state.tool_ledger is not None and invocation_key is not None:
        try:
            await state.await_work(
                state.tool_ledger.complete(
                    ToolExecutionOutcome(
                        invocation_key=invocation_key, content=result, success=True
                    )
                )
            )
        except Exception as exc:
            message = f"Error: tool outcome not durable: {type(exc).__name__}"
            bus.emit("tool.error", {"name": tc.name, "error": type(exc).__name__})
            return tool_result(tc.id, message), False
    result_digest, result_size = _digest_and_size(result)
    end_data: dict[str, Any] = {
        "name": tc.name,
        "result_length": len(result),
        "duration_ms": duration_ms,
        "result_digest": result_digest,
        "result_size": result_size,
    }
    # The result body rides the event only under raw-capture (NFR-2); by default
    # it stays out of memory and the spool entirely.
    if bus.store_raw_bodies:
        end_data["result"] = result
    # A tool may annotate its own call (ctx.tool_extra) — a small opaque scalar
    # dict the spool always keeps (it is signal, not a body).
    if ctx.tool_extra:
        end_data["tool_extra"] = dict(ctx.tool_extra)
    bus.emit("tool.end", end_data)

    state.tool_calls_made += 1
    return tool_result(tc.id, result), True
