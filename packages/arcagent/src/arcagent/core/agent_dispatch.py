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

import asyncio
import contextlib
import hashlib
import logging
import uuid
import weakref
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import arcrun
from arcprompt import PromptSource
from arctrust.causal import UNATTRIBUTED

from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.provider import WORKSPACE_ROOT, AgentCapabilityProvider, _Skill
from arcagent.core import known_channels, turn_context
from arcagent.core.agent_lifecycle import activate_runtime_bindings
from arcagent.core.context_prep import (
    ContextPrep,
    estimate_tokens,
    prepare_context,
    spool_run_event,
)
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal import AssembledPrompt, SessionManager, wire_messages
from arcagent.core.session_internal.capability_ledger import (
    bind_session_id,
    current_session_id,
    reset_session_id,
)
from arcagent.core.telemetry import AgentTelemetry, TelemetryAuditSink
from arcagent.tools._policy_fill import resolve_run_budget
from arcagent.tools.approval_policy import narrowed_loop_controls
from arcagent.utils.causality import agent_scope, turn_root

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

_logger = logging.getLogger("arcagent.agent_dispatch")


@dataclass(frozen=True)
class RunContext:
    """Everything a turn needs before its first model call.

    ``turn`` is the per-turn material stored with the user's message (Context
    prep retrieval first, then live state such as connection status); it rides
    after the cached system prefix. ``strategy`` is the pre-selected strategy the
    run is pinned to (``None`` when the caller pins its own, as resume does).
    """

    telemetry: AgentTelemetry
    bus: ModuleBus
    model: Any
    provider: AgentCapabilityProvider
    prompt: AssembledPrompt
    bridge: Callable[[arcrun.Event], None]
    prompt_source: PromptSource
    turn: str
    strategy: arcrun.StrategyChoice | None


async def build_run_context(
    agent: ArcAgent,
    task: str,
    *,
    run_id: str,
    session: SessionManager | None = None,
    allowed_strategies: list[str] | None = None,
    choose: bool = True,
) -> RunContext:
    """Prepare a turn: strategy, stable system context, retrieval, in that trace order.

    The steps, as the run trace shows them before the first model call:

    1. **Strategy** — picked from cheap inputs (the request and the last two
       exchanges) on ``[arcrun] strategy_model``, CONCURRENTLY with retrieval:
       neither waits for the other, and retrieval can never sway the choice.
    2. **System context** — the stable, cached prefix (identity, policy, the
       capability manifest, guides, ``context.md``). Nothing per-turn goes in it.
    3. **Retrieval** — :func:`prepare_context`, the one bounded pass.
    4. **Session** — the history the run will carry.

    Assembles the capability surface (ADR-023) and emits ``agent:pre_respond``
    (with the retrieved text as ``insight``) before returning.
    """
    from arcagent.core.model_manager import create_arcrun_bridge

    telemetry, tool_registry, context, bus = agent._ensure_started()
    model = agent._ensure_model()

    invoke_tools = tool_registry.to_arcrun_tools()

    # Freeze the complete prompt set for this run and emit one provenance event
    # (COMP-006 / REQ-123, REQ-132). The snapshot backs an overlay-aware source
    # so an operator override reaches the model — and is attributable to exact
    # bytes. Absent a resolver (bare/test agent), the shipped prompts are used.
    prompt_source = _run_prompt_source(agent, telemetry)

    # ``base`` is the harness-level preamble that opens every agent's prompt,
    # above its own identity; like every other prompt it is operator-overridable.
    harness_sections = {"base": prompt_source.resolve("arcagent", "base_system")}

    strategy: arcrun.StrategyChoice | None = None
    if choose:
        strategy, prep = await asyncio.gather(
            _choose_strategy(agent, task, session, allowed_strategies, prompt_source),
            prepare_context(agent, task),
        )
    else:
        prep = await prepare_context(agent, task)

    # Orchestration: spawn_task is context-dependent (reads depth/budget from the
    # loop's ToolContext), so it is dispatched directly, not routed through the
    # context-free invoke() path. Children inherit spawn + the invoke tools.
    ctx_tools: list[Any] = []
    if agent._config.spawn.enabled:
        from arcagent.orchestration import RootTokenBudget, make_spawn_tool

        spawn_guidance = prompt_source.resolve("arcagent", "spawn_guidance")
        child_sections = {**harness_sections, "spawn_guidance": spawn_guidance}

        async def child_system_prompt() -> str:
            # Assembled only if the model spawns: a turn that never does pays for
            # one prompt assembly, not two (every assembly re-runs every hook).
            child = await context.assemble_system_prompt(
                agent._workspace, extra_sections=child_sections, prompt_source=prompt_source
            )
            return child.as_text()

        child_tools = list(invoke_tools)  # closure ref — append makes children see spawn
        # Shared cross-child token pool (LLM10) — one per run, capping the
        # aggregate spend of every child the model spawns this turn.
        pool_total = agent._config.spawn.max_total_tokens
        root_token_budget = RootTokenBudget(pool_total) if pool_total else None
        spawn_tool = make_spawn_tool(
            model=model,
            tools=child_tools,
            system_prompt_factory=child_system_prompt,
            spawn_timeout_seconds=agent._config.spawn.timeout_seconds,
            max_concurrent_spawns=agent._config.spawn.max_concurrent,
            # A spawned child gets its OWN turn cap ([spawn].max_turns, default 50)
            # — a child does one bounded sub-task, lower than the agent's own cap.
            # Without threading it here it fell back to a hardcoded 25 and truncated
            # mid-task, its max_turns breach then painted "Error" after real work.
            max_child_turns=agent._config.spawn.max_turns,
            root_token_budget=root_token_budget,
            prompt_source=prompt_source,
        )
        child_tools.append(spawn_tool)
        ctx_tools = [spawn_tool]
        harness_sections = {**harness_sections, "spawn_guidance": spawn_guidance}

    prompt = await context.assemble_system_prompt(
        agent._workspace,
        extra_sections=harness_sections,
        prompt_source=prompt_source,
    )
    turn = "\n\n".join(part for part in (prep.text, prompt.turn) if part)
    if choose:
        _record_context_prep(agent, run_id, strategy, prompt, prep, session, turn)
    # The turn's origin channel is read HERE, in the dispatch task that bound it
    # a few lines earlier, and travels stamped on every progress event the bridge
    # forwards. A consumer therefore never has to work out where to answer.
    bridge = create_arcrun_bridge(
        bus,
        model_id=agent._config.llm.model,
        agent_label=agent._config.agent.name,
        task_supervisor=agent._background_tasks,
        reply_target=turn_context.inbound_channel(),
        bridge_only_tools=frozenset(tool.name for tool in ctx_tools),
        take_tool_outcome=tool_registry.take_tool_outcome,
        session_id=current_session_id(),
        agent_did=agent._identity.did if agent._identity else "",
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
        skill_files=agent._skill_files,
    )

    # ``insight`` is this turn's retrieved context, handed to the skills improver
    # (and any other pre-respond reader) so nothing retrieves a second time.
    await bus.emit("agent:pre_respond", {"task": task, "insight": prep.text})
    return RunContext(
        telemetry=telemetry,
        bus=bus,
        model=model,
        provider=provider,
        prompt=prompt,
        bridge=bridge,
        prompt_source=prompt_source,
        turn=turn,
        strategy=strategy,
    )


#: Earlier session messages the strategy selector sees: the last two exchanges.
_STRATEGY_RECENT_MESSAGES = 4


async def _choose_strategy(
    agent: ArcAgent,
    task: str,
    session: SessionManager | None,
    allowed: list[str] | None,
    prompt_source: PromptSource,
) -> arcrun.StrategyChoice:
    """The turn's strategy from cheap inputs only, on the configured strategy model."""
    recent: list[str] = []
    if session is not None:
        earlier = session.get_messages()[:-1]  # the request itself is ``task``
        recent = [
            f"{m.get('role', '')}: {arcrun.content_text(m.get('content'))}"
            for m in earlier[-_STRATEGY_RECENT_MESSAGES:]
            if m.get("role") in ("user", "assistant")
        ]
    registry = agent._tool_registry
    return await arcrun.choose_strategy(
        allowed,
        agent._ensure_strategy_model(),
        task=task,
        recent=recent,
        tool_names=sorted(registry.tools) if registry is not None else (),
        prompt_source=prompt_source,
        timeout=agent._config.arcrun.strategy_timeout_seconds,
    )


def _record_context_prep(
    agent: ArcAgent,
    run_id: str,
    strategy: arcrun.StrategyChoice | None,
    prompt: AssembledPrompt,
    prep: ContextPrep,
    session: SessionManager | None,
    turn: str,
) -> None:
    """Write the pre-model steps to the run trace in the order the request is built.

    Strategy, then the stable system context (with per-tier hashes, and whether
    the prefix is byte-identical to this session's previous turn so the provider
    cache can hit), then retrieval and any skipped step, then the session.
    """
    if strategy is not None:
        spool_run_event(
            agent,
            run_id,
            "strategy.selected",
            {
                "strategy": strategy.name,
                "reason": strategy.reason,
                "selected_by": strategy.selected_by,
                "latency_ms": strategy.latency_ms,
            },
        )
    tiers = {
        "session": _sha256(prompt.session),
        "run": _sha256(prompt.run),
    }
    prefix = _sha256("\n".join(prompt.segments))
    previous = _LAST_PREFIX.get(session) if session is not None else None
    if session is not None:
        _LAST_PREFIX[session] = prefix
    spool_run_event(
        agent,
        run_id,
        "context.system",
        {
            "cached": previous == prefix,
            "tokens": estimate_tokens(prompt.as_text()),
            "sha256": prefix,
            "tiers": {
                name: {"sha256": digest, "tokens": estimate_tokens(text)}
                for (name, digest), text in zip(
                    tiers.items(), (prompt.session, prompt.run), strict=True
                )
            },
        },
    )
    spool_run_event(
        agent, run_id, "context.retrieval", prep.trace, outcome=str(prep.trace.get("status"))
    )
    for skipped in prep.skipped:
        spool_run_event(agent, run_id, "context.skipped", dict(skipped))
    messages = session.get_messages() if session is not None else []
    spool_run_event(
        agent,
        run_id,
        "context.session",
        {
            "turns": sum(1 for m in messages if m.get("role") == "user"),
            "messages": len(messages),
            "tokens": estimate_tokens(
                "".join(arcrun.content_text(m.get("content")) for m in messages)
            )
            + estimate_tokens(turn),
        },
    )


#: The last system prefix hash per live session, so the trace can say whether a
#: turn's cached prefix still matches the one before it. Weak: it goes with the
#: session object.
_LAST_PREFIX: weakref.WeakKeyDictionary[SessionManager, str] = weakref.WeakKeyDictionary()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_prompt_source(agent: ArcAgent, telemetry: AgentTelemetry) -> PromptSource:
    """Build this run's overlay-aware prompt source + emit the provenance event.

    Freezes the complete prompt set once (REQ-123) and audits it once
    (REQ-132); a rejected override raises here, before the run starts, so the
    run fails closed. When the agent has no resolver (bare/test construction
    that skipped capability setup) this is the stock source, so a minimal agent
    still assembles its prompt.
    """
    from arcprompt import ResolverPromptSource, StockPromptSource

    resolver = agent._prompt_resolver
    if resolver is None:
        return StockPromptSource()

    from arcstore.spool import current_request_id

    from arcagent.core.prompt_context import snapshot_run_prompts

    actor_did = agent._identity.did if agent._identity else "did:arc:unknown"
    snapshot = snapshot_run_prompts(
        resolver,
        actor_did=actor_did,
        audit_event=telemetry.audit_event,
        request_id=current_request_id(),
        workspace=agent._workspace,
    )
    return ResolverPromptSource(snapshot)


def _agent_skills(agent: ArcAgent) -> list[_Skill]:
    """Registered skills as lean specs — retired ones are already suppressed from _skills."""
    registry = agent._capability_registry
    if registry is None:
        return []
    return [
        _Skill(
            name=e.name,
            description=e.description,
            location=e.location,
            scan_root=e.scan_root,
            read_current=e.read_current,
            bundle_folder=e.bundle_folder,
        )
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
    agent: ArcAgent,
    session_id: str,
    *,
    interactive: bool = False,
    handle_observer: Callable[[arcrun.RunHandle], None] | None = None,
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
        agent._run_coordinator.register(session_id, handle, interactive=interactive)
        if handle_observer is not None:
            handle_observer(handle)

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
    interactive: bool = False,
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
    turn_context.set_interactive(interactive)
    if reply_target and turn_context.mail_conversation(reply_target) is None:
        # Remember this channel so arcui can offer it as a delivery-target
        # dropdown (a raw chat_id exists only here on the inbound path). A mail
        # thread is not a channel: it takes one reply and is never a target.
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
    interactive: bool = False,
    on_handle: Callable[[arcrun.RunHandle], None] | None = None,
    content: list[dict[str, Any]] | None = None,
    on_behalf_of: str | None = None,
) -> AsyncGenerator[arcrun.StreamEvent, None]:
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

    ``on_behalf_of`` is the principal a signed request names (the channel user,
    the schedule's approver, the teammate who sent mail). The turn is its own
    causal root performed by the agent; absent a signed principal it acts for
    whoever its caller bound (see :func:`arcagent.utils.causality.turn_root`).
    """
    events: asyncio.Queue[arcrun.StreamEvent] = asyncio.Queue(maxsize=1)

    async def produce() -> None:
        async with agent._run_coordinator.turn(session.session_id):
            async with contextlib.aclosing(
                _dispatch_stream_locked(
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
                    interactive=interactive,
                    on_handle=on_handle,
                    content=content,
                    on_behalf_of=on_behalf_of,
                )
            ) as stream:
                async for event in stream:
                    await events.put(event)

    producer = agent._background_tasks.create(produce(), name=f"agent_stream:{session.session_id}")
    try:
        while True:
            if not events.empty():
                yield events.get_nowait()
                continue
            if producer.done():
                await producer
                break
            get_event = asyncio.create_task(events.get())
            try:
                done, _pending = await asyncio.wait(
                    {get_event, producer}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_event in done:
                    yield get_event.result()
            finally:
                if not get_event.done():
                    get_event.cancel()
                    await asyncio.gather(get_event, return_exceptions=True)
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


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
    interactive: bool,
    on_handle: Callable[[arcrun.RunHandle], None] | None,
    content: list[dict[str, Any]] | None,
    overheard: bool = False,
    on_behalf_of: str | None = None,
) -> AsyncGenerator[arcrun.StreamEvent, None]:
    """Execute a turn after its session serialization lock is held."""
    agent._ensure_started()
    activate_runtime_bindings(agent)
    bind_inbound_channel(
        agent, reply_target, reply_label, overheard=overheard, interactive=interactive
    )
    # One run id spans prompt assembly AND the loop, bound here so a step that runs
    # while the prompt is built — a memory recall, most of all — lands in the same
    # run's trace as the reads that follow it. arcrun reuses this id when handed in,
    # so the two halves share one timeline instead of assembly falling outside it.
    run_id = run_id or str(uuid.uuid4())
    with turn_root(_agent_did(agent), run_id, on_behalf_of=on_behalf_of):
        # The person's words are durable before recall or any model call, so a
        # reader who returns mid-turn finds their own message in the history.
        await session.append_message({"role": "user", "content": content or input_text})
        controls = narrowed_loop_controls(agent, session, allowed_strategies)
        try:
            with agent._queue_run_context(session.session_id, run_id):
                run_ctx = await build_run_context(
                    agent,
                    input_text,
                    run_id=run_id,
                    session=session,
                    allowed_strategies=controls["allowed_strategies"],
                )
        except Exception as exc:
            record_not_started(agent, run_id, f"turn preparation failed ({type(exc).__name__})")
            raise
        telemetry, bus, model = run_ctx.telemetry, run_ctx.bus, run_ctx.model
        provider, prompt, bridge = run_ctx.provider, run_ctx.prompt, run_ctx.bridge
        prompt_source = run_ctx.prompt_source
        if run_ctx.strategy is not None:
            controls["allowed_strategies"] = [run_ctx.strategy.name]
        await session.attach_turn_context(run_ctx.turn)
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
        on_handle, untrack_run = track_active_run(
            agent, session.session_id, interactive=interactive, handle_observer=on_handle
        )
        # Bind the session id for this dispatch so the capability ledger (and the
        # per-agent egress proxy) key trifecta legs to THIS session (SPEC-035).
        session_token = bind_session_id(session.session_id)
        try:
            async with telemetry.session_span(input_text):
                _logger.info("Running agent loop for task: %s", input_text[:80])
                with agent._queue_run_context(session.session_id, run_id):
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
                        # Tag background self-wakes (pulse / scheduler / consolidation /
                        # sub-agent) so the dashboard can badge them apart from real,
                        # person-driven runs. Absent origin == interactive (SPEC D-726).
                        run_origin=None if turn_context.interactive() else "background",
                        store_raw_bodies=agent._config.telemetry.capture_tool_io,
                        max_tokens=run_max_tokens,
                        max_cost_usd=run_max_cost_usd,
                        run_id=run_id,
                        audit_sink=TelemetryAuditSink(telemetry),
                        on_handle=on_handle,
                        prompt_source=prompt_source,
                        **controls,
                    )
                    async with contextlib.aclosing(
                        cast(AsyncGenerator[arcrun.StreamEvent, None], raw_stream)
                    ):
                        async for event in raw_stream:
                            if isinstance(event, arcrun.TurnEndEvent):
                                final_text = event.final_text
                            yield event
        except Exception as exc:  # reason: re-raise after log
            await bus.emit(
                "agent:error",
                {"task": input_text, "error": str(exc), "error_type": type(exc).__name__},
            )
            # A run that overflowed or otherwise errored still left the session
            # bloated — and the success-path compaction below never runs on this
            # branch, so an over-limit session used to stay permanently over the
            # max (every subsequent turn re-overflowed and never pruned). Compact
            # here too, best-effort, so the next turn starts under the limit. Never
            # mask the original error.
            with contextlib.suppress(Exception):
                await maybe_compact(agent, session, run_id=run_id)
            raise
        finally:
            reset_session_id(session_token)
            untrack_run()

        await session.append_message({"role": "assistant", "content": final_text})
        await maybe_compact(agent, session, run_id=run_id)
        await bus.emit(
            "agent:post_respond",
            {
                "result": None,
                "messages": [
                    {"role": "user", "content": input_text},
                    {"role": "assistant", "content": final_text},
                ],
                "session_id": session.session_id,
                "run_id": run_id,
                "automated": False,
            },
        )


def record_not_started(agent: ArcAgent, run_id: str, reason: str) -> None:
    """Close a run that died before its model loop began, so it never reads "running".

    arcrun writes the terminal ``loop.complete`` only for a loop it started; a
    turn that failed in preparation, or was abandoned by the turn-start bound,
    otherwise left only its pre-model trace rows and showed "Running" forever.
    """
    spool_run_event(agent, run_id, "run.not_started", {"reason": reason}, outcome="failed")


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
    on_behalf_of: str | None = None,
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
    run_id = str(uuid.uuid4())
    await agent._run_coordinator.acquire_turn(coordination_key)
    try:
        agent._ensure_started()
        activate_runtime_bindings(agent)
        # This path is the turn an inbound human message opens (registered
        # interactive=True below), so it counts as real interaction for memory.
        bind_inbound_channel(
            agent, reply_target, reply_label, overheard=overheard, hop=hop, interactive=True
        )
        with (
            turn_root(_agent_did(agent), run_id, on_behalf_of=on_behalf_of),
            agent._queue_run_context(session.session_id, run_id),
        ):
            await session.append_message({"role": "user", "content": content or input_text})
            controls = narrowed_loop_controls(agent, session, None)
            try:
                run_ctx = await build_run_context(
                    agent,
                    input_text,
                    run_id=run_id,
                    session=session,
                    allowed_strategies=controls["allowed_strategies"],
                )
            except Exception as exc:
                record_not_started(
                    agent, run_id, f"turn preparation failed ({type(exc).__name__})"
                )
                raise
            model, provider, prompt = run_ctx.model, run_ctx.provider, run_ctx.prompt
            bridge, prompt_source = run_ctx.bridge, run_ctx.prompt_source
            if run_ctx.strategy is not None:
                controls["allowed_strategies"] = [run_ctx.strategy.name]
            await session.attach_turn_context(run_ctx.turn)
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
                    run_id=run_id,
                    prompt_source=prompt_source,
                    **controls,
                )
            finally:
                reset_session_id(session_token)
    except BaseException:
        agent._run_coordinator.release_turn(coordination_key)
        raise
    agent._run_coordinator.register(coordination_key, handle, interactive=True)
    finalizer = agent._background_tasks.create(
        _finalize_tracked_run(agent, handle, session, coordination_key, input_text, run_id),
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
    run_id: str,
) -> None:
    """Await a tracked run, commit its assistant turn, compact, and untrack it."""
    final_text = ""
    try:
        result = await handle.result()
        final_text = result.content or ""
        await session.append_message({"role": "assistant", "content": final_text})
        await maybe_compact(agent, session, run_id=run_id)
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
                    "run_id": run_id,
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


async def maybe_compact(
    agent: ArcAgent, session: SessionManager, *, run_id: str | None = None
) -> None:
    """Trigger a discrete compaction when the current context ratio crosses the
    compact threshold. Uses the estimate over live messages (context_ratio),
    which reflects real context size and drops after a boundary (debounce)."""
    context = agent._context
    if context is None:
        return
    # S001 SDD ladder: >compact_threshold summarize (LLM); else >prune_threshold
    # mask stale tool outputs (cheap, no LLM). Both are discrete, persisted
    # boundaries so the reclaimed baseline stays prompt-cache-stable.
    ratio = session.context_ratio()
    cfg = agent._config.context
    if ratio >= cfg.compact_threshold:
        eval_model = agent._ensure_model()
        evaluation_id = str(uuid.uuid4())
        with (
            agent_scope(_agent_did(agent), evaluation_id),
            agent._queue_run_context(
                session.session_id,
                evaluation_id,
                origin="compaction",
                parent_run_id=run_id,
            ),
        ):
            await session.compact(eval_model)
    elif ratio >= cfg.prune_threshold:
        await session.prune()


def _agent_did(agent: ArcAgent) -> str:
    """The DID the agent acts as; unattributed (loudly) before identity exists."""
    identity = agent._identity
    return identity.did if identity is not None and identity.did else UNATTRIBUTED
