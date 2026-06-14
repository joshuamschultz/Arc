"""Agent dispatch — the single streaming ``run`` body.

Sibling of ``arcagent.core.agent``. Owns the per-call orchestration:
prepare a run context (system prompt assembly + spawn-tool attachment +
bus event emission), drive arcrun's streaming loop, and yield
``StreamEvent``s. There is exactly one dispatch path (SPEC-027) — no
blocking/async/chat fork.

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
from arcrun import run_stream as arcrun_run_stream

from arcagent.capabilities.provider import WORKSPACE_ROOT, AgentCapabilityProvider, _Skill
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
    AgentCapabilityProvider,  # the unified capability surface for arcrun
    str,  # system_prompt
    Callable[[Event], None],  # bridge
]:
    """Prepare shared run context for the streaming run.

    Assembles the agent's capabilities into an ``AgentCapabilityProvider``
    (ADR-023): policy-wrapped registry tools (invocable) + the agent's skills
    (lazily loaded) + spawn (dispatched with live context). Merges strategy and
    orchestration prompt guidance into the system prompt. Emits
    ``agent:pre_respond`` before returning.
    """
    from arcagent.core.model_manager import create_arcrun_bridge

    telemetry, tool_registry, context, bus = agent._ensure_started()
    model = agent._ensure_model()

    invoke_tools = tool_registry.to_arcrun_tools()

    # Strategy prompt guidance — arcrun-owned strategies and tools.
    tool_names = [t.name for t in invoke_tools]
    strategy_sections = get_strategy_prompts(tool_names=tool_names)

    # Orchestration: spawn_task is context-dependent (reads depth/budget from the
    # loop's ToolContext), so it is dispatched directly, not routed through the
    # context-free invoke() path. Children inherit spawn + the invoke tools.
    ctx_tools: list[Any] = []
    if agent._config.spawn.enabled:
        from arcagent.orchestration import SPAWN_GUIDANCE, make_spawn_tool

        child_system_prompt = await context.assemble_system_prompt(
            agent._workspace,
            extra_sections={**strategy_sections, "spawn_guidance": SPAWN_GUIDANCE},
        )
        child_tools = list(invoke_tools)  # closure ref — append makes children see spawn
        spawn_tool = make_spawn_tool(
            model=model,
            tools=child_tools,
            system_prompt=child_system_prompt,
            spawn_timeout_seconds=agent._config.spawn.timeout_seconds,
            max_concurrent_spawns=agent._config.spawn.max_concurrent,
        )
        child_tools.append(spawn_tool)
        ctx_tools = [spawn_tool]
        strategy_sections = {**strategy_sections, "spawn_guidance": SPAWN_GUIDANCE}

    system_prompt = await context.assemble_system_prompt(
        agent._workspace, extra_sections=strategy_sections
    )
    bridge = create_arcrun_bridge(
        bus,
        model_id=agent._config.llm.model,
        agent_label=agent._config.agent.name,
    )

    provider = AgentCapabilityProvider(
        tools=invoke_tools,
        ctx_tools=ctx_tools,
        skills=_agent_skills(agent),
        tier=str(agent._config.security.tier),
        caller_did=agent._identity.did if agent._identity else "did:arc:unknown",
        workspace_authored=_workspace_authored(agent),
    )

    await bus.emit("agent:pre_respond", {"task": task})
    return telemetry, bus, model, provider, system_prompt, bridge


def _agent_skills(agent: ArcAgent) -> list[_Skill]:
    """Snapshot the agent's registered skills as lean, loadable specs."""
    registry = agent._capability_registry
    if registry is None:
        return []
    return [
        _Skill(
            name=entry.name,
            description=entry.description,
            location=entry.location,
            scan_root=entry.scan_root,
        )
        for entry in registry._skills.values()
    ]


def _workspace_authored(agent: ArcAgent) -> frozenset[str]:
    """Names of capabilities (tools + skills) the agent authored at runtime.

    These live under ``<workspace>/capabilities`` (scan_root == "workspace")
    and are denied in federal tier (AC-6.1). Pulled from the capability registry,
    which records each entry's scan_root.
    """
    registry = agent._capability_registry
    if registry is None:
        return frozenset()
    names = {name for name, entry in registry._tools.items() if entry.scan_root == WORKSPACE_ROOT}
    names |= {
        name for name, entry in registry._skills.items() if entry.scan_root == WORKSPACE_ROOT
    }
    return frozenset(names)


async def dispatch_stream(
    agent: ArcAgent,
    input_text: str,
    *,
    session: SessionManager,
) -> AsyncIterator[StreamEvent]:
    """The single execution path: stream one agent turn into a session.

    Appends the user turn, drives arcrun's streaming loop with the session's
    history (parity with the old ``chat``), yields every ``StreamEvent``, then
    commits the assistant turn and runs compaction. The recording bridge is
    handed to ``run_stream`` as ``on_event`` so SPEC-026 spool/WORM capture and
    module telemetry fire exactly as they did on the blocking path.

    Emits ``agent:pre_respond`` (via ``build_run_context``) before the loop and
    ``agent:post_respond`` after the stream is fully consumed.
    """
    await session.append_message({"role": "user", "content": input_text})
    telemetry, bus, model, provider, system_prompt, bridge = await build_run_context(
        agent, input_text
    )
    history = [Message(**m) for m in session.get_messages()]
    transform = agent._context.transform_context if agent._context else None

    final_text = ""
    try:
        async with telemetry.session_span(input_text):
            _logger.info("Running agent loop for task: %s", input_text[:80])
            raw_stream = await arcrun_run_stream(
                model=model,
                capabilities=provider,
                system_prompt=system_prompt,
                task=input_text,
                messages=history,
                on_event=bridge,
                transform_context=transform,
                actor_did=agent._identity.did if agent._identity else None,
                store_raw_bodies=agent._config.telemetry.capture_tool_io,
            )
            async for event in raw_stream:
                if isinstance(event, TurnEndEvent):
                    final_text = event.final_text
                yield event
    except Exception as exc:  # reason: re-raise after log
        await bus.emit(
            "agent:error",
            {"task": input_text, "error": str(exc), "error_type": type(exc).__name__},
        )
        raise

    await session.append_message({"role": "assistant", "content": final_text})
    await maybe_compact(agent, session)
    await bus.emit(
        "agent:post_respond",
        {
            "result": None,
            "messages": [
                {"role": "user", "content": input_text},
                {"role": "assistant", "content": final_text},
            ],
            "session_id": session.session_id,
            "automated": False,
        },
    )


async def maybe_compact(agent: ArcAgent, session: SessionManager) -> None:
    """Trigger compaction if context ratio exceeds compact_threshold."""
    context = agent._context
    if context is None:
        return
    ratio = session.token_ratio()
    if ratio >= agent._config.context.compact_threshold:
        eval_model = agent._ensure_model()
        await session.compact(eval_model, agent._workspace)
