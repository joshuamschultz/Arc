"""Spawn tool — recursively start a child run() for task decomposition.

Security considerations (ASI-01, ASI-08, LLM10, NIST AU-2/AU-3):
- System prompt override: child inherits parent's prompt as immutable preamble
- Timeout: wall-clock limit on child execution prevents unbounded consumption
- Audit: spawn.start and spawn.complete events emitted for every child
- Error sanitization: internal details logged, generic message returned to LLM
- Concurrency: max_concurrent_spawns limits parallel child runs
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

from arcrun.events import Event, EventBus
from arcrun.state import RunState
from arcrun.types import SandboxConfig, Tool, ToolContext

_logger = logging.getLogger("arcrun.builtins.spawn")

# Sensible defaults for resource limits
_DEFAULT_SPAWN_TIMEOUT_SECONDS = 300
_DEFAULT_MAX_CONCURRENT_SPAWNS = 5
_DEFAULT_MAX_CHILD_TURNS = 25
_MAX_ERROR_LEN = 200


def _make_bubble_handler(child_run_id: str, parent_bus: EventBus) -> Callable[[Event], None]:
    """Create on_event callback that bubbles child events to parent bus."""

    def handler(event: Event) -> None:
        parent_bus.emit(
            f"child.{child_run_id}.{event.type}",
            {**event.data, "child_run_id": child_run_id},
        )

    return handler


def make_spawn_tool(
    *,
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    state: RunState,
    sandbox: SandboxConfig | None = None,
    allowed_strategies: list[str] | None = None,
    spawn_timeout_seconds: int = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    max_concurrent_spawns: int = _DEFAULT_MAX_CONCURRENT_SPAWNS,
    max_child_turns: int = _DEFAULT_MAX_CHILD_TURNS,
) -> Tool:
    """Create a spawn_task tool that starts a child run().

    Factory captures parent context via closure, same pattern as make_execute_tool.
    """
    # Semaphore limits concurrent child runs (ASI-08, LLM10)
    spawn_semaphore = asyncio.Semaphore(max_concurrent_spawns)

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        # Depth guard
        if state.depth >= state.max_depth:
            return f"Error: max spawn depth ({state.max_depth}) reached"

        child_task = params["task"]
        requested_tools = params.get("tools")

        # System prompt policy (ASI-01): parent prompt is always prepended
        # as an immutable preamble. LLM can specialize but not replace
        # core behavioral rules.
        child_specialization = params.get("system_prompt")
        if child_specialization:
            child_system_prompt = (
                f"{system_prompt}\n\n"
                f"--- Child Specialization ---\n{child_specialization}"
            )
            prompt_overridden = True
        else:
            child_system_prompt = system_prompt
            prompt_overridden = False

        # Resolve tool subset
        if requested_tools is not None:
            resolved: list[Tool] = []
            for name in requested_tools:
                matched = next((t for t in tools if t.name == name), None)
                if matched is None:
                    return f"Error: unknown tool '{name}'"
                resolved.append(matched)
            child_tools = resolved
        else:
            child_tools = list(tools)

        child_run_id = str(uuid.uuid4())
        bubble_handler = _make_bubble_handler(child_run_id, state.event_bus)

        # Audit event: spawn start (NIST AU-2/AU-3)
        state.event_bus.emit("spawn.start", {
            "child_run_id": child_run_id,
            "parent_run_id": state.run_id,
            "parent_depth": state.depth,
            "system_prompt_overridden": prompt_overridden,
            "tool_subset": [t.name for t in child_tools],
            "max_child_turns": max_child_turns,
            "timeout_seconds": spawn_timeout_seconds,
        })

        # Import here to avoid circular import at module level
        from arcrun.loop import run

        try:
            async with spawn_semaphore:
                result = await asyncio.wait_for(
                    run(
                        model,
                        child_tools,
                        child_system_prompt,
                        child_task,
                        max_turns=max_child_turns,
                        depth=state.depth + 1,
                        max_depth=state.max_depth,
                        on_event=bubble_handler,
                        sandbox=sandbox,
                        allowed_strategies=allowed_strategies,
                    ),
                    timeout=spawn_timeout_seconds,
                )

            # Audit event: spawn complete
            state.event_bus.emit("spawn.complete", {
                "child_run_id": child_run_id,
                "parent_run_id": state.run_id,
                "turns_used": result.turns,
                "cost_usd": result.cost_usd,
                "success": True,
            })

            return result.content or "(no content)"

        except asyncio.TimeoutError:
            _logger.warning(
                "Child run %s timed out after %ds",
                child_run_id,
                spawn_timeout_seconds,
            )
            state.event_bus.emit("spawn.complete", {
                "child_run_id": child_run_id,
                "parent_run_id": state.run_id,
                "success": False,
                "error": "timeout",
            })
            return f"Error: child task timed out after {spawn_timeout_seconds}s"

        except Exception as exc:
            # Log full details internally, return sanitized message to LLM
            _logger.warning(
                "Child run %s failed: %s: %s",
                child_run_id,
                type(exc).__name__,
                str(exc)[:_MAX_ERROR_LEN],
            )
            state.event_bus.emit("spawn.complete", {
                "child_run_id": child_run_id,
                "parent_run_id": state.run_id,
                "success": False,
                "error": type(exc).__name__,
            })
            # Sanitized error — no internal details leaked to LLM (LLM02)
            return "Error: child task failed"

    return Tool(
        name="spawn_task",
        description=(
            "Spawn a child agent to accomplish a sub-task. The child runs "
            "independently and returns its result. Use for task decomposition "
            "and parallel work."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the child agent to accomplish",
                },
                "system_prompt": {
                    "type": "string",
                    "description": (
                        "Optional specialization prompt appended to the parent's "
                        "system prompt. Cannot replace core behavioral rules."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of tool names the child can use. "
                        "If omitted, inherits all parent tools."
                    ),
                },
            },
            "required": ["task"],
        },
        execute=_execute,
        timeout_seconds=spawn_timeout_seconds,
    )
