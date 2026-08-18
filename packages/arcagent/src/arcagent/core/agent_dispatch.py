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
import uuid
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import arcrun
from arcstore.spool import request_context

from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.provider import WORKSPACE_ROOT, AgentCapabilityProvider, _Skill
from arcagent.core import known_channels, turn_context
from arcagent.core.agent_lifecycle import activate_runtime_bindings
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal import AssembledPrompt, SessionManager, wire_messages
from arcagent.core.session_internal.capability_ledger import bind_session_id, reset_session_id
from arcagent.core.telemetry import AgentTelemetry
from arcagent.tools._policy_fill import resolve_run_budget
from arcagent.tools.approval_policy import narrowed_loop_controls

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
    AssembledPrompt,  # tiered system prompt + this turn's context
    Callable[[arcrun.Event], None],  # bridge
]:
    """Prepare shared run context for the streaming run.

    Assembles the agent's capabilities into an ``AgentCapabilityProvider``
    (ADR-023): policy-wrapped registry tools (invocable) + the agent's skills
    (lazily loaded) + spawn (dispatched with live context). Merges the harness
    preamble, strategy, and orchestration guidance into the system prompt. Emits
    ``agent:pre_respond`` before returning.
    """
    from arcagent.core.model_manager import create_arcrun_bridge

    telemetry, tool_registry, context, bus = agent._ensure_started()
    model = agent._ensure_model()

    invoke_tools = tool_registry.to_arcrun_tools()

    # Freeze the complete prompt set for this run and emit one provenance event
    # (COMP-006 / REQ-123, REQ-132). The snapshot backs an overlay-aware resolver
    # so an operator override reaches the model — and is attributable to exact
    # bytes. Absent a resolver (bare/test agent), fall back to stock-only loading.
    resolve = _run_prompt_resolve(agent, telemetry)

    # Strategy prompt guidance — arcrun-owned strategies and tools. ``base`` is
    # the harness-level preamble that opens every agent's prompt, above its own
    # identity; like every other prompt it is operator-overridable via arcprompt.
    tool_names = [t.name for t in invoke_tools]
    strategy_sections = {
        "base": resolve("arcagent", "base_system"),
        **arcrun.get_strategy_prompts(tool_names=tool_names, resolve=resolve),
    }

    # Orchestration: spawn_task is context-dependent (reads depth/budget from the
    # loop's ToolContext), so it is dispatched directly, not routed through the
    # context-free invoke() path. Children inherit spawn + the invoke tools.
    ctx_tools: list[Any] = []
    if agent._config.spawn.enabled:
        from arcagent.orchestration import RootTokenBudget, make_spawn_tool

        spawn_guidance = resolve("arcagent", "spawn_guidance")
        child_prompt = await context.assemble_system_prompt(
            agent._workspace,
            extra_sections={**strategy_sections, "spawn_guidance": spawn_guidance},
        )
        child_system_prompt = child_prompt.as_text()
        child_tools = list(invoke_tools)  # closure ref — append makes children see spawn
        # Shared cross-child token pool (LLM10) — one per run, capping the
        # aggregate spend of every child the model spawns this turn.
        pool_total = agent._config.spawn.max_total_tokens
        root_token_budget = RootTokenBudget(pool_total) if pool_total else None
        spawn_tool = make_spawn_tool(
            model=model,
            tools=child_tools,
            system_prompt=child_system_prompt,
            spawn_timeout_seconds=agent._config.spawn.timeout_seconds,
            max_concurrent_spawns=agent._config.spawn.max_concurrent,
            root_token_budget=root_token_budget,
        )
        child_tools.append(spawn_tool)
        ctx_tools = [spawn_tool]
        strategy_sections = {**strategy_sections, "spawn_guidance": spawn_guidance}

    prompt = await context.assemble_system_prompt(
        agent._workspace, extra_sections=strategy_sections, query=task
    )
    # The turn's origin channel is read HERE, in the dispatch task that bound it
    # a few lines earlier, and travels stamped on every progress event the bridge
    # forwards. A consumer therefore never has to work out where to answer.
    bridge = create_arcrun_bridge(
        bus,
        model_id=agent._config.llm.model,
        agent_label=agent._config.agent.name,
        task_supervisor=agent._background_tasks,
        reply_target=turn_context.inbound_channel(),
    )

    provider = AgentCapabilityProvider(
        tools=invoke_tools,
        ctx_tools=ctx_tools,
        skills=_agent_skills(agent),
        tier=str(agent._config.security.tier),
        caller_did=agent._identity.did if agent._identity else "did:arc:unknown",
        workspace_authored=_workspace_authored(agent),
        requires_skill=_requires_skill_map(agent),
        audit=telemetry.audit_event,
    )

    await bus.emit("agent:pre_respond", {"task": task})
    return telemetry, bus, model, provider, prompt, bridge


def _run_prompt_resolve(agent: ArcAgent, telemetry: AgentTelemetry) -> Callable[[str, str], str]:
    """Build this run's overlay-aware prompt resolver + emit the provenance event.

    Freezes the complete prompt set once (REQ-123) and audits it once
    (REQ-132). When the agent has no resolver (bare/test construction that
    skipped capability setup) this degrades to stock-only ``load_stock`` so a
    minimal agent still assembles its prompt.
    """
    from arcprompt import load_stock

    resolver = agent._prompt_resolver
    if resolver is None:
        return load_stock

    from arcagent.core.prompt_context import snapshot_resolver, snapshot_run_prompts

    actor_did = agent._identity.did if agent._identity else "did:arc:unknown"
    snapshot = snapshot_run_prompts(
        resolver, actor_did=actor_did, audit_event=telemetry.audit_event
    )
    return snapshot_resolver(snapshot)


def _agent_skills(agent: ArcAgent) -> list[_Skill]:
    """Registered skills as lean specs — retired ones are already suppressed from _skills."""
    registry = agent._capability_registry
    if registry is None:
        return []
    return [
        _Skill(name=e.name, description=e.description, location=e.location, scan_root=e.scan_root)
        for e in registry.skill_entries()
    ]


def _requires_skill_map(agent: ArcAgent) -> dict[str, str]:
    """Tool name -> the skill that teaches it (R-014), for the tools that declare
    one. Sourced from the capability registry's tool metadata — the core
    ToolRegistry's ``RegisteredTool`` drops ``requires_skill``, so the capability
    layer is the source of truth. The provider activates these on invoke (U13)."""
    registry = agent._capability_registry
    if registry is None:
        return {}
    return {
        entry.meta.name: entry.meta.requires_skill
        for entry in registry.tool_entries()
        if entry.meta.requires_skill
    }


def _workspace_authored(agent: ArcAgent) -> frozenset[str]:
    """Names of capabilities (tools + skills) the agent authored at runtime.

    These live under ``<workspace>/capabilities`` (scan_root == "workspace")
    and are denied in federal tier (AC-6.1). Pulled from the capability registry,
    which records each entry's scan_root.
    """
    registry = agent._capability_registry
    if not isinstance(registry, CapabilityRegistry):
        return frozenset()
    return registry.workspace_authored_names(WORKSPACE_ROOT)


def _tighter(a: float | None, b: float | None) -> float | None:
    """The lower of two ceilings; None means unbounded on that side."""
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def track_active_run(
    agent: ArcAgent, session_id: str
) -> tuple[Callable[[arcrun.RunHandle], None], Callable[[], None]]:
    """Register a streaming run's live handle so the operator kill-switch can reach it.

    Returns ``(on_handle, untrack)``: pass ``on_handle`` to ``arcrun.run_stream`` so
    the loop's :class:`RunHandle` lands in ``agent._active_runs`` (keyed by session
    id — the operator's ``session_key``; the watcher also matches by ``run_id``),
    and call ``untrack`` in a ``finally`` to remove it. The removal is
    identity-guarded so a re-entrant run for the same session is never evicted by a
    stale finalizer. This is the streaming-path parity for what ``start_tracked_run``
    already does for tracked runs (GAP-A).

    Registered as background: a streaming run was not opened by an inbound
    message, so a delivered message must open its own turn rather than join it
    (REQ-303). Only :func:`start_tracked_run` registers an injection target.
    """
    registered: list[arcrun.RunHandle] = []

    def on_handle(handle: arcrun.RunHandle) -> None:
        registered.append(handle)
        agent._run_coordinator.register(session_id, handle, interactive=False)

    def untrack() -> None:
        if registered:
            agent._run_coordinator.unregister(session_id, registered[0])

    return on_handle, untrack


def bind_inbound_channel(
    agent: ArcAgent,
    reply_target: str | None,
    reply_label: str | None,
    *,
    overheard: bool = False,
    hop: int = 0,
) -> None:
    """Bind the turn's inbound channel and remember it as a delivery target.

    Called at every turn-dispatch entry, before the loop task is created, so the
    contextvar reaches the tool dispatches inside the loop (a capability like the
    scheduler defaults a new schedule's delivery to the channel the request
    arrived on). ``reply_target`` is None for non-channel runs.
    """
    turn_context.set_inbound_channel(reply_target)
    # Bound here, beside the channel, for the same reason: a contextvar set across
    # the executor->agent task boundary does not reliably reach the loop's hooks.
    turn_context.set_overheard(overheard)
    turn_context.set_inbound_hop(hop)
    if reply_target:
        # Remember this channel so arcui can offer it as a delivery-target
        # dropdown (a raw chat_id exists only here on the inbound path).
        known_channels.record(
            agent._workspace, target=reply_target, label=reply_label or reply_target
        )


async def dispatch_stream(
    agent: ArcAgent,
    input_text: str,
    *,
    session: SessionManager,
    tool_choice: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
    run_id: str | None = None,
    reply_target: str | None = None,
    reply_label: str | None = None,
    allowed_strategies: list[str] | None = None,
) -> AsyncIterator[arcrun.StreamEvent]:
    """The single execution path: stream one agent turn into a session.

    Appends the user turn, drives arcrun's streaming loop with the session's
    history (parity with the old ``chat``), yields every ``StreamEvent``, then
    commits the assistant turn and runs compaction. The recording bridge is
    handed to ``run_stream`` as ``on_event`` so SPEC-026 spool/WORM capture and
    module telemetry fire exactly as they did on the blocking path.

    ``tool_choice`` is forwarded verbatim to ``arcrun_run_stream`` so callers
    that need to force a tool call on the first turn (e.g. orchestrators
    chaining stages through a ``signals_completion`` tool) can pin behavior
    without reaching into arcrun.

    Emits ``agent:pre_respond`` (via ``build_run_context``) before the loop and
    ``agent:post_respond`` after the stream is fully consumed.

    ``reply_target`` (the inbound channel for interactive turns) is bound to the
    per-turn context here — the same entry point that replays module runtime
    bindings — so tool dispatches inside the loop inherit it (e.g. the scheduler
    defaults a new schedule's delivery to this channel).
    """
    # Serialize the *whole* turn. In particular, history cannot be read by a
    # later call until this call has committed its assistant response.
    async with agent._run_coordinator.turn(session.session_id):
        async for event in _dispatch_stream_locked(
            agent,
            input_text,
            session=session,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
            run_id=run_id,
            reply_target=reply_target,
            reply_label=reply_label,
            allowed_strategies=allowed_strategies,
        ):
            yield event


async def _dispatch_stream_locked(
    agent: ArcAgent,
    input_text: str,
    *,
    session: SessionManager,
    tool_choice: dict[str, Any] | None,
    max_tokens: int | None,
    max_cost_usd: float | None,
    run_id: str | None,
    reply_target: str | None,
    reply_label: str | None,
    allowed_strategies: list[str] | None,
    overheard: bool = False,
) -> AsyncIterator[arcrun.StreamEvent]:
    """Execute a turn after its session serialization lock is held."""
    agent._ensure_started()
    activate_runtime_bindings(agent)
    bind_inbound_channel(agent, reply_target, reply_label, overheard=overheard)
    # One run id spans prompt assembly AND the loop, bound here so a step that runs
    # while the prompt is built — a memory recall, most of all — lands in the same
    # run's trace as the reads that follow it. arcrun reuses this id when handed in,
    # so the two halves share one timeline instead of assembly falling outside it.
    run_id = run_id or str(uuid.uuid4())
    with request_context(run_id):
        run_ctx = await build_run_context(agent, input_text)
        telemetry, bus, model, provider, prompt, bridge = run_ctx
        await session.append_message(prompt.session_record(input_text))
        history = wire_messages(session.get_messages(), workspace=agent._workspace)
        transform = agent._context.transform_context if agent._context else None
        # SPEC-038 F1 — resolve the tier-resolved per-run budget so the arcrun
        # circuit-breaker (LLM10) is reachable through the real streaming path.
        # SPEC-040 F2 — a caller-pinned per-run budget (a planner step's slice of
        # the plan aggregate) tightens it; the lower ceiling always wins.
        cfg_tokens, cfg_cost = resolve_run_budget(agent._config)
        run_max_tokens = _tighter(cfg_tokens, max_tokens)
        run_max_tokens = int(run_max_tokens) if run_max_tokens is not None else None
        run_max_cost_usd = _tighter(cfg_cost, max_cost_usd)

        final_text = ""
        # Expose the streaming run's handle so the operator kill-switch can cancel it.
        on_handle, untrack_run = track_active_run(agent, session.session_id)
        # Bind the session id for this dispatch so the capability ledger (and the
        # per-agent egress proxy) key trifecta legs to THIS session (SPEC-035).
        session_token = bind_session_id(session.session_id)
        try:
            async with telemetry.session_span(input_text):
                _logger.info("Running agent loop for task: %s", input_text[:80])
                raw_stream = await arcrun.run_stream(
                    model=model,
                    capabilities=provider,
                    system_prompt=prompt.segments,
                    task=input_text,
                    messages=history,
                    on_event=bridge,
                    transform_context=transform,
                    tool_choice=tool_choice,
                    actor_did=agent._identity.did if agent._identity else None,
                    store_raw_bodies=agent._config.telemetry.capture_tool_io,
                    max_tokens=run_max_tokens,
                    max_cost_usd=run_max_cost_usd,
                    run_id=run_id,
                    on_handle=on_handle,
                    **narrowed_loop_controls(agent, session, allowed_strategies),
                )
                async for event in raw_stream:
                    if isinstance(event, arcrun.TurnEndEvent):
                        final_text = event.final_text
                    yield event
        except Exception as exc:  # reason: re-raise after log
            await bus.emit(
                "agent:error",
                {"task": input_text, "error": str(exc), "error_type": type(exc).__name__},
            )
            raise
        finally:
            reset_session_id(session_token)
            untrack_run()

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


async def start_tracked_run(
    agent: ArcAgent,
    input_text: str,
    *,
    session_key: str,
    reply_target: str | None = None,
    reply_label: str | None = None,
    overheard: bool = False,
    hop: int = 0,
    content: list[dict[str, Any]] | None = None,
) -> arcrun.RunHandle:
    """Start an async, steerable run for ``session_key`` and track its handle.

    Mirrors :func:`dispatch_stream`'s session bookkeeping but drives
    ``arcrun.run_async`` so a :class:`RunHandle` exists for the duration of the
    loop — the seam SPEC-031 mid-task delivery injects into. The handle is
    registered in ``agent._active_runs`` and removed by a finalizer that commits
    the assistant turn, compacts, and emits ``agent:post_respond`` (parity with
    the streaming path). REQ-040/041.

    This is the turn an inbound message opens, so the run is registered as an
    injection target: the next message for this session joins it rather than
    starting a second one (REQ-302). ``reply_target`` / ``reply_label`` carry the
    channel the message arrived on, same as the streaming path.

    ``content`` is the turn as blocks when the message carried an artefact
    (SPEC-065): it is what gets stored, and ``wire_messages`` materialises its
    references for the loop call. ``input_text`` remains the text projection —
    the prompt query, the audit line and the post-respond record.
    """
    session = await agent.session(session_key)
    coordination_key = session.session_id
    await agent._run_coordinator.acquire_turn(coordination_key)
    try:
        agent._ensure_started()
        activate_runtime_bindings(agent)
        bind_inbound_channel(agent, reply_target, reply_label, overheard=overheard, hop=hop)
        _telemetry, _bus, model, provider, prompt, bridge = await build_run_context(
            agent, input_text
        )
        await session.append_message(prompt.session_record(content or input_text))
        history = wire_messages(session.get_messages(), workspace=agent._workspace)
        transform = agent._context.transform_context if agent._context else None
        max_tokens, max_cost_usd = resolve_run_budget(agent._config)

        # Bind the session id before the loop task is created so the background
        # run (and its tool dispatches) inherit it in their copied context.
        session_token = bind_session_id(session.session_id)
        try:
            handle = await arcrun.run_async(
                model,
                provider,
                prompt.segments,
                input_text,
                messages=history,
                on_event=bridge,
                transform_context=transform,
                actor_did=agent._identity.did if agent._identity else None,
                store_raw_bodies=agent._config.telemetry.capture_tool_io,
                max_tokens=max_tokens,
                max_cost_usd=max_cost_usd,
                **narrowed_loop_controls(agent, session, None),
            )
        finally:
            reset_session_id(session_token)
    except BaseException:
        agent._run_coordinator.release_turn(coordination_key)
        raise
    agent._run_coordinator.register(coordination_key, handle, interactive=True)
    finalizer = agent._background_tasks.create(
        _finalize_tracked_run(agent, handle, session, coordination_key, input_text),
        name=f"run_finalizer:{coordination_key}",
    )
    agent._run_finalizers.add(finalizer)
    finalizer.add_done_callback(agent._run_finalizers.discard)
    return handle


async def _finalize_tracked_run(
    agent: ArcAgent,
    handle: arcrun.RunHandle,
    session: SessionManager,
    session_key: str,
    input_text: str,
) -> None:
    """Await a tracked run, commit its assistant turn, compact, and untrack it."""
    final_text = ""
    try:
        result = await handle.result()
        final_text = result.content or ""
        await session.append_message({"role": "assistant", "content": final_text})
        await maybe_compact(agent, session)
        if agent._bus is not None:
            await agent._bus.emit(
                "agent:post_respond",
                {
                    "result": None,
                    "messages": [
                        {"role": "user", "content": input_text},
                        {"role": "assistant", "content": final_text},
                    ],
                    "session_id": session.session_id,
                    "automated": True,
                },
            )
    except Exception:  # reason: fail-open — background run must not crash the loop
        _logger.exception("Tracked run finalizer failed for session %s", session_key)
    finally:
        coordinator = getattr(agent, "_run_coordinator", None)
        if coordinator is not None:
            coordinator.unregister(session_key, handle)
            coordinator.release_turn(session_key)
        elif agent._active_runs.get(session_key) is handle:
            # Compatibility for focused tests using a minimal mock agent.
            del agent._active_runs[session_key]


async def maybe_compact(agent: ArcAgent, session: SessionManager) -> None:
    """Trigger a discrete compaction when the current context ratio crosses the
    compact threshold. Uses the estimate over live messages (context_ratio),
    which reflects real context size and drops after a boundary (debounce)."""
    context = agent._context
    if context is None:
        return
    ratio = session.context_ratio()
    if ratio >= agent._config.context.compact_threshold:
        eval_model = agent._ensure_model()
        await session.compact(eval_model)
