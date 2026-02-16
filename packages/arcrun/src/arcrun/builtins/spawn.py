"""Spawn tool — recursively start a child run() for task decomposition."""

from __future__ import annotations

import uuid
from typing import Any, Callable

from arcrun.events import Event, EventBus
from arcrun.state import RunState
from arcrun.types import SandboxConfig, Tool, ToolContext


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
) -> Tool:
    """Create a spawn_task tool that starts a child run().

    Factory captures parent context via closure, same pattern as make_execute_tool.
    """

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        # Depth guard
        if state.depth >= state.max_depth:
            return f"Error: max spawn depth ({state.max_depth}) reached"

        child_task = params["task"]
        child_system_prompt = params.get("system_prompt", system_prompt)
        requested_tools = params.get("tools")

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

        # Import here to avoid circular import at module level
        from arcrun.loop import run

        try:
            result = await run(
                model,
                child_tools,
                child_system_prompt,
                child_task,
                depth=state.depth + 1,
                max_depth=state.max_depth,
                on_event=bubble_handler,
                sandbox=sandbox,
                allowed_strategies=allowed_strategies,
            )
            return result.content or "(no content)"
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {str(exc)[:200]}"

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
                        "Optional system prompt to specialize the child's role. "
                        "If omitted, inherits parent's."
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
        timeout_seconds=None,
    )
