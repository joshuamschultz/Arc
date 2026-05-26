"""Agent dispatch helpers — run/chat/run_async/chat_async/chat_stream bodies.

Sibling of ``arcagent.core.agent``. Owns the per-call orchestration
that prepares a run context (system prompt assembly + spawn-tool
attachment + bus event emission), drives the underlying ArcRun
entry points, and wraps the result.

Functions take an ``agent`` parameter (the ArcAgent instance). They
read its private attributes and call its small accessors —
intentional coupling, since this is internal helper code split out
solely to keep ``agent.py`` slim.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from arcllm import Message
from arcrun import Event, StreamEvent, TurnEndEvent, get_strategy_prompts
from arcrun import run as arcrun_run
from arcrun import run_async as arcrun_run_async
from arcrun import run_stream as arcrun_run_stream

from arcagent.core.agent_handle import AgentHandle, _build_messages_dict
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal import SessionManager
from arcagent.core.telemetry import AgentTelemetry

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

_logger = logging.getLogger("arcagent.agent_dispatch")


async def build_run_context(
    agent: ArcAgent, task: str
) -> tuple[
    AgentTelemetry,
    ModuleBus,
    Any,  # model
    list[Any],  # arcrun tools
    str,  # system_prompt
    Callable[[Event], None],  # bridge
]:
    """Prepare shared run context for both blocking and async paths.

    Returns the common objects needed to invoke arcrun.
    Assembles the agent-side toolkit: registry tools + (optionally)
    spawn_task. Merges strategy and orchestration prompt guidance
    into the system prompt so the model knows when/how to use
    each capability. Emits agent:pre_respond before returning.
    """
    from arcagent.core.model_manager import create_arcrun_bridge

    telemetry, tool_registry, context, bus = agent._ensure_started()
    model = agent._ensure_model()

    tools = tool_registry.to_arcrun_tools()

    # Strategy prompt guidance — arcrun-owned strategies and tools.
    tool_names = [t.name for t in tools]
    strategy_sections = get_strategy_prompts(tool_names=tool_names)

    # Orchestration: register spawn_task if config enables it.
    # Closure mutation lets children inherit spawn_task — the tool's
    # closure captures ``tools`` by reference, so appending after
    # construction makes spawn visible to nested children too.
    if agent._config.spawn.enabled:
        from arcagent.orchestration import SPAWN_GUIDANCE, make_spawn_tool

        # Children get the same orchestration guidance so nested
        # decomposition behaves consistently.
        child_system_prompt = await context.assemble_system_prompt(
            agent._workspace,
            extra_sections={
                **strategy_sections,
                "spawn_guidance": SPAWN_GUIDANCE,
            },
        )
        tools = list(tools)  # mutable for closure-mutation pattern
        spawn_tool = make_spawn_tool(
            model=model,
            tools=tools,  # closure ref — append below makes children see spawn too
            system_prompt=child_system_prompt,
            spawn_timeout_seconds=agent._config.spawn.timeout_seconds,
            max_concurrent_spawns=agent._config.spawn.max_concurrent,
        )
        tools.append(spawn_tool)
        strategy_sections = {**strategy_sections, "spawn_guidance": SPAWN_GUIDANCE}

    system_prompt = await context.assemble_system_prompt(
        agent._workspace, extra_sections=strategy_sections
    )
    bridge = create_arcrun_bridge(
        bus,
        model_id=agent._config.llm.model,
        agent_label=agent._config.agent.name,
    )

    await bus.emit("agent:pre_respond", {"task": task})
    return telemetry, bus, model, tools, system_prompt, bridge


async def execute_loop(
    agent: ArcAgent,
    task: str,
    *,
    messages: list[Any] | None = None,
    tool_choice: dict[str, Any] | None = None,
    automated: bool = False,
) -> Any:
    """Blocking execution: build tools, emit events, run loop."""
    telemetry, bus, model, tools, system_prompt, bridge = await build_run_context(agent, task)
    try:
        async with telemetry.session_span(task):
            _logger.info("Running agent loop for task: %s", task[:80])
            result = await arcrun_run(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                task=task,
                messages=messages,
                on_event=bridge,
                transform_context=agent._context.transform_context if agent._context else None,
                tool_choice=tool_choice,
            )
    except Exception as exc:  # reason: re-raise after log
        await bus.emit(
            "agent:error",
            {"task": task, "error": str(exc), "error_type": type(exc).__name__},
        )
        raise

    session_id = agent._session.session_id if agent._session else ""
    messages_dict = _build_messages_dict(task, result, messages)
    await bus.emit(
        "agent:post_respond",
        {
            "result": result,
            "messages": messages_dict,
            "session_id": session_id,
            "automated": automated,
        },
    )
    return result


async def execute_loop_async(
    agent: ArcAgent,
    task: str,
    *,
    messages: list[Message] | None = None,
    tool_choice: dict[str, Any] | None = None,
    session: SessionManager | None = None,
    automated: bool = False,
) -> AgentHandle:
    """Non-blocking execution: returns handle for steering.

    Telemetry span wrapping is caller-responsibility since the
    handle controls the execution lifetime.
    """
    telemetry, bus, model, tools, system_prompt, bridge = await build_run_context(agent, task)
    context = agent._context
    session_id = agent._session.session_id if agent._session else ""
    try:
        handle = await arcrun_run_async(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            task=task,
            messages=messages,
            on_event=bridge,
            transform_context=context.transform_context if context else None,
            tool_choice=tool_choice,
        )
    except Exception as exc:  # reason: re-raise after log
        await bus.emit(
            "agent:error",
            {"task": task, "error": str(exc), "error_type": type(exc).__name__},
        )
        raise
    return AgentHandle(
        handle=handle,
        bus=bus,
        telemetry=telemetry,
        session_id=session_id,
        task=task,
        messages=messages,
        session=session,
        automated=automated,
    )


async def prepare_chat_session(
    agent: ArcAgent, message: str, session_id: str | None
) -> SessionManager:
    """Ensure session exists and append the user turn.

    Raises RuntimeError if agent is not started.
    """
    if agent._session is None:
        msg = "Agent not started. Call startup() first."
        raise RuntimeError(msg)
    session = agent._session

    if session_id is not None:
        await session.resume_session(session_id)
    elif not session.session_id:
        await session.create_session()

    await session.append_message({"role": "user", "content": message})
    return session


async def maybe_compact(agent: ArcAgent, session: SessionManager) -> None:
    """Trigger compaction if context ratio exceeds compact_threshold."""
    context = agent._context
    if context is None:
        return
    ratio = session.token_ratio()
    if ratio >= agent._config.context.compact_threshold:
        eval_model = agent._ensure_model()
        await session.compact(eval_model, agent._workspace)


async def chat_stream(
    agent: ArcAgent,
    task: str,
    *,
    tool_choice: dict[str, Any] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream a task through the agent loop, yielding incremental events.

    Returns an ``AsyncIterator[StreamEvent]`` that yields:
    - :class:`~arcrun.streams.TokenEvent` for each response fragment
    - :class:`~arcrun.streams.TurnEndEvent` as the final event

    Emits ``agent:pre_respond`` before the loop starts and
    ``agent:post_respond`` after the stream is fully consumed.
    The caller must iterate the returned iterator to completion for
    the post-respond event to fire. Use ``chat()`` for blocking callers.
    """
    _telemetry, tool_registry, context, bus = agent._ensure_started()
    model = agent._ensure_model()

    tools = tool_registry.to_arcrun_tools()
    tool_names = [t.name for t in tools]

    strategy_sections = get_strategy_prompts(tool_names=tool_names)

    if agent._config.spawn.enabled:
        from arcagent.orchestration import SPAWN_GUIDANCE, make_spawn_tool

        child_system_prompt = await context.assemble_system_prompt(
            agent._workspace,
            extra_sections={
                **strategy_sections,
                "spawn_guidance": SPAWN_GUIDANCE,
            },
        )
        tools = list(tools)
        spawn_tool = make_spawn_tool(
            model=model,
            tools=tools,
            system_prompt=child_system_prompt,
            spawn_timeout_seconds=agent._config.spawn.timeout_seconds,
            max_concurrent_spawns=agent._config.spawn.max_concurrent,
        )
        tools.append(spawn_tool)
        strategy_sections = {**strategy_sections, "spawn_guidance": SPAWN_GUIDANCE}

    system_prompt = await context.assemble_system_prompt(
        agent._workspace, extra_sections=strategy_sections
    )

    await bus.emit("agent:pre_respond", {"task": task})

    raw_stream = await arcrun_run_stream(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        task=task,
    )

    async def _wrapped_stream() -> AsyncIterator[StreamEvent]:
        """Wrap raw stream to emit post-respond after all events are consumed."""
        final_text = ""
        async for event in raw_stream:
            if isinstance(event, TurnEndEvent):
                final_text = event.final_text
            yield event
        await bus.emit(
            "agent:post_respond",
            {
                "result": None,
                "messages": [
                    {"role": "user", "content": task},
                    {"role": "assistant", "content": final_text},
                ],
                "session_id": agent._session.session_id if agent._session else "",
            },
        )

    return _wrapped_stream()
