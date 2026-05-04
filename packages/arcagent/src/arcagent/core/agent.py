"""Agent orchestrator — wires all components, invokes ArcRun.

ArcAgent is the top-level class that owns all core components.
It initializes them in dependency order, bridges ArcRun events
to the Module Bus, and manages the full lifecycle.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from arcllm import Message
from arcrun import Event, RunHandle, StreamEvent, TurnEndEvent, get_strategy_prompts
from arcrun import run as arcrun_run
from arcrun import run_async as arcrun_run_async
from arcrun import run_stream as arcrun_run_stream
from arctrust import AgentIdentity

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry, SkillEntry
from arcagent.core.config import ArcAgentConfig
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import EventContext, ModuleBus
from arcagent.core.session_internal import ContextManager, SessionManager
from arcagent.core.settings_manager import SettingsManager
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import (
    RegisteredTool,
    ToolRegistry,
    ToolTransport,
)
from arcagent.utils import load_eval_model

_logger = logging.getLogger("arcagent.agent")

# Per D-346/D-347 — short skill-usage instruction injected at priority 91.
_SKILL_USAGE_INSTRUCTION = (
    "## Skills\n"
    "When the manifest above lists a relevant skill, read its SKILL.md "
    "for step-by-step guidance before invoking the related tools."
)


def _validate_vault_backend(backend_ref: str) -> None:
    """Validate vault backend module reference format.

    Must be ``module.path:ClassName`` format. Prevents injection
    of arbitrary strings into importlib.
    """
    if ":" not in backend_ref:
        raise ConfigError(
            code="CONFIG_INVALID_VAULT_BACKEND",
            message=f"Invalid vault backend format (missing ':'): {backend_ref}",
            details={"backend": backend_ref},
        )

    module_path, _ = backend_ref.rsplit(":", 1)
    if not module_path or ".." in module_path:
        raise ConfigError(
            code="CONFIG_INVALID_VAULT_BACKEND",
            message=f"Invalid vault backend module path: {module_path}",
            details={"backend": backend_ref},
        )


def create_arcrun_bridge(
    bus: ModuleBus,
    *,
    model_id: str = "",
    agent_label: str = "",
) -> Callable[[Event], None]:
    """Create on_event callback for arcrun.run().

    Maps ArcRun events to Module Bus events:
      tool.start  → agent:pre_tool
      tool.end    → agent:post_tool
      turn.start  → agent:pre_plan
      turn.end    → agent:post_plan
      llm.call    → llm:call_complete

    ArcRun's on_event is synchronous (Callable[[Event], None]),
    so we schedule the async bus.emit via the running event loop.
    Enriches llm.call events with the actual model name and agent label.
    """
    _event_map = {
        "tool.start": "agent:pre_tool",
        "tool.end": "agent:post_tool",
        "turn.start": "agent:pre_plan",
        "turn.end": "agent:post_plan",
        "llm.call": "llm:call_complete",
    }
    # Hold strong references to pending tasks so they aren't GC'd
    _pending: set[asyncio.Task[Any]] = set()

    # Extract provider and model name from provider/model format
    _provider = model_id.split("/", 1)[0] if "/" in model_id else "unknown"
    _model_name = model_id.split("/", 1)[1] if "/" in model_id else model_id

    def bridge(event: Event) -> None:
        bus_event = _event_map.get(event.type)
        if bus_event is not None:
            # Always copy to a plain dict — Event.data is typed as
            # MappingProxyType[Any, Any] (read-only) by arcrun; ModuleBus.emit
            # requires dict[str, Any]. Shallow copy is intentional here.
            data: dict[str, Any] = dict(event.data)
            # Enrich llm.call events with actual model/provider/agent
            if event.type == "llm.call":
                data["model"] = _model_name
                data["provider"] = _provider
                if agent_label:
                    data["agent_label"] = agent_label
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, data))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            except RuntimeError:
                _logger.warning(
                    "No running event loop for bridge event: %s",
                    event.type,
                )

    return bridge


def create_arcllm_bridge(bus: ModuleBus) -> Callable[[Any], None]:
    """Create on_event callback for ArcLLM's load_model().

    Maps ArcLLM TraceRecord event_types to Module Bus events:
      llm_call       → llm:call_complete
      config_change  → llm:config_change
      circuit_change → llm:circuit_change

    ArcLLM's on_event is synchronous (Callable[[TraceRecord], None]),
    so we schedule the async bus.emit via the running event loop.
    Accepts both TraceRecord (Pydantic) and plain dict inputs.
    """
    _event_map = {
        "llm_call": "llm:call_complete",
        "config_change": "llm:config_change",
        "circuit_change": "llm:circuit_change",
    }
    # Hold strong references to pending tasks so they aren't GC'd
    _pending: set[asyncio.Task[Any]] = set()

    def bridge(record: Any) -> None:
        data = record.model_dump() if hasattr(record, "model_dump") else record
        event_type = data.get("event_type", "")
        bus_event = _event_map.get(event_type)
        if bus_event is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, data))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            except RuntimeError:
                _logger.warning(
                    "No running event loop for LLM bridge event: %s",
                    event_type,
                )

    return bridge


def _build_messages_dict(
    task: str, result: Any, messages: list[Any] | None
) -> list[dict[str, Any]]:
    """Serialize messages for agent:post_respond bus event.

    Uses model_dump() for Pydantic models; falls back to raw dict.
    Synthesizes a minimal exchange when no message history exists
    so memory modules can process single-turn run() calls.
    """
    if messages:
        return [m.model_dump() if hasattr(m, "model_dump") else m for m in messages]
    response_text = getattr(result, "content", None) or ""
    return [
        {"role": "user", "content": task},
        {"role": "assistant", "content": response_text},
    ]


_MAX_STEERING_MESSAGE_LEN = 32_768  # 32 KiB — generous but bounded


def _validate_steering_message(message: str) -> None:
    """Validate a steer/follow_up message.

    Prevents empty or oversized payloads from reaching the queue.
    """
    if not message or not message.strip():
        msg = "Steering message must not be empty"
        raise ValueError(msg)
    if len(message) > _MAX_STEERING_MESSAGE_LEN:
        msg = f"Steering message exceeds {_MAX_STEERING_MESSAGE_LEN} character limit"
        raise ValueError(msg)


class AgentHandle:
    """Control interface for a running agent execution.

    Wraps ArcRun's RunHandle to add agent-level lifecycle:
    bus events (agent:post_respond), audit trail, and session
    management are deferred until result() is awaited.

    Args:
        handle: The underlying ArcRun RunHandle.
        bus: Module bus for emitting agent-layer events.
        telemetry: Telemetry instance for audit events.
        session_id: Current session identifier (empty for run_async).
        task: The original task string.
        messages: Session message history, or None for single-turn.
        session: SessionManager for committing results (chat_async only).
    """

    def __init__(
        self,
        handle: RunHandle,
        bus: ModuleBus,
        telemetry: AgentTelemetry,
        session_id: str,
        task: str,
        messages: list[Message] | None,
        session: SessionManager | None = None,
    ) -> None:
        self._handle = handle
        self._bus = bus
        self._telemetry = telemetry
        self._session_id = session_id
        self._task = task
        self._messages = messages
        self._session = session
        self._result_consumed = False
        self._completed = False

    async def steer(self, message: str) -> None:
        """Interrupt current execution with new direction."""
        self._check_not_completed("steer")
        _validate_steering_message(message)
        await self._handle.steer(message)
        self._telemetry.audit_event(
            "agent.steer",
            {"session_id": self._session_id, "message_len": len(message)},
        )

    async def follow_up(self, message: str) -> None:
        """Queue a follow-up message for end of current turn."""
        self._check_not_completed("follow_up")
        _validate_steering_message(message)
        await self._handle.follow_up(message)
        self._telemetry.audit_event(
            "agent.follow_up",
            {"session_id": self._session_id, "message_len": len(message)},
        )

    async def cancel(self) -> None:
        """Cancel execution. Returns partial result via result()."""
        self._check_not_completed("cancel")
        await self._handle.cancel()
        self._telemetry.audit_event(
            "agent.cancel",
            {"session_id": self._session_id},
        )

    async def result(self) -> Any:
        """Await completion and emit agent:post_respond.

        May only be called once. Raises RuntimeError on repeat calls
        to prevent duplicate bus events and session side effects.
        Commits assistant message and runs compaction when a session
        is attached (chat_async path).
        """
        if self._result_consumed:
            msg = "AgentHandle.result() has already been awaited"
            raise RuntimeError(msg)
        self._result_consumed = True

        loop_result = await self._handle.result()
        self._completed = True

        messages_dict = _build_messages_dict(self._task, loop_result, self._messages)
        await self._bus.emit(
            "agent:post_respond",
            {"result": loop_result, "messages": messages_dict, "session_id": self._session_id},
        )

        # Commit assistant response to session (mirrors chat() blocking path)
        if self._session is not None:
            response_text = getattr(loop_result, "content", None) or ""
            await self._session.append_message({"role": "assistant", "content": response_text})

        return loop_result

    @property
    def state(self) -> Any:
        """Read-only access to RunState."""
        return self._handle.state

    def _check_not_completed(self, method: str) -> None:
        """Raise if execution has already completed."""
        if self._completed:
            msg = f"Cannot call {method}() after result() has been awaited"
            raise RuntimeError(msg)


class ArcAgent:
    """Top-level agent orchestrator.

    Owns all core components and manages their lifecycle.
    """

    def __init__(self, config: ArcAgentConfig, *, config_path: Path | None = None) -> None:
        self._config = config
        self._config_path = config_path or Path("arcagent.toml")

        # Resolve workspace path relative to config file, not cwd
        workspace_path = Path(config.agent.workspace)
        if not workspace_path.is_absolute() and config_path:
            self._workspace = (config_path.parent / workspace_path).resolve()
        else:
            self._workspace = workspace_path.resolve()

        self._reload_lock = asyncio.Lock()
        self._started = False

        # Components initialized during startup()
        self._telemetry: AgentTelemetry | None = None
        self._identity: AgentIdentity | None = None
        self._bus: ModuleBus | None = None
        self._tool_registry: ToolRegistry | None = None
        self._context: ContextManager | None = None
        self._session: SessionManager | None = None
        self._capability_registry: CapabilityRegistry | None = None
        self._capability_loader: CapabilityLoader | None = None
        self._settings: SettingsManager | None = None
        self._vault_resolver: Any = None
        self._model: Any = None
        # Names of tools currently registered in ToolRegistry that came
        # from the capability loader. Tracked so reload() can drop them
        # cleanly and re-register the latest set.
        self._capability_tool_names: set[str] = set()

    async def startup(self) -> None:
        """Initialize all components in dependency order.

        1. Vault resolver (if configured)
        2. Telemetry
        3. Identity
        4. Module Bus
        5. Tool Registry
        6. Context Manager
        7. Emit agent:init
        """
        # 1. Vault resolver (optional)
        if self._config.vault.backend:
            self._vault_resolver = self._create_vault_resolver()

        # 2. Telemetry (uses placeholder DID until identity is ready)
        self._telemetry = AgentTelemetry(
            config=self._config.telemetry,
            agent_did="pending",
        )

        # 3. Identity — config file is the single source of truth for DID
        self._identity = AgentIdentity.from_config(
            self._config.identity,
            vault_resolver=self._vault_resolver,
            org=self._config.agent.org,
            agent_type=self._config.agent.type,
            config_path=self._config_path,
        )

        # Update telemetry with real DID (avoids full reconstruction)
        self._telemetry.set_agent_did(self._identity.did)

        # 4. Module Bus
        self._bus = ModuleBus()

        # 5. Tool Registry
        self._tool_registry = ToolRegistry(
            config=self._config.tools,
            bus=self._bus,
            telemetry=self._telemetry,
        )

        workspace = self._workspace
        workspace.mkdir(parents=True, exist_ok=True)

        # 6. Context Manager
        self._context = ContextManager(
            config=self._config.context,
            telemetry=self._telemetry,
            bus=self._bus,
        )

        # 7. Session Manager (owns context)
        self._session = SessionManager(
            config=self._config.session,
            context_config=self._config.context,
            telemetry=self._telemetry,
            workspace=workspace,
            context_manager=self._context,
        )

        # 8. Settings Manager
        self._settings = SettingsManager(
            config=self._config,
            telemetry=self._telemetry,
            bus=self._bus,
            config_path=self._config_path,
        )

        # 9. Capability subsystem (replaces SkillRegistry, ExtensionLoader,
        # MODULE.yaml-based module loading, and the hardcoded built-in
        # tool list — SPEC-021 unified capability surface).
        await self._setup_capabilities(workspace)

        # 10. Mark started BEFORE emitting agent:ready so capabilities that
        # immediately invoke agent.run() (e.g. scheduler) don't hit
        # the _ensure_started() guard.
        self._started = True

        # 11. Emit agent:ready with deferred-binding callbacks. Hooks
        # subscribed to this event receive agent.run/chat callbacks
        # without core knowing about them.
        await self._bus.emit(
            "agent:ready",
            {
                "run_fn": self.run,
                "chat_fn": self.chat,
                "run_async_fn": self.run_async,
                "chat_async_fn": self.chat_async,
            },
        )

        await self._bus.emit("agent:init", {"config": self._config.agent.name})
        _logger.info(
            "Agent %s started (DID: %s)",
            self._config.agent.name,
            self._identity.did,
        )

    def _ensure_started(
        self,
    ) -> tuple[AgentTelemetry, ToolRegistry, ContextManager, ModuleBus]:
        """Validate agent is started and return narrowed component references."""
        if (
            not self._started
            or self._telemetry is None
            or self._tool_registry is None
            or self._context is None
            or self._bus is None
        ):
            msg = "Agent not started. Call startup() first."
            raise RuntimeError(msg)
        return self._telemetry, self._tool_registry, self._context, self._bus

    def _ensure_model(self) -> Any:
        """Load and cache model on first use.

        Passes a JSONLTraceStore so every LLM call is persisted to
        ``<agent_root>/traces/`` for historical UI display and audit.
        Per ``arcllm.JSONLTraceStore`` (NIST AU-9), traces live OUTSIDE
        the workspace tool sandbox — the trace store wants the agent
        root, not the workspace subdirectory. arcui's federated trace
        store reads from the same location, so this mismatch was the
        cause of "I chatted but no session shows" symptoms.

        Wires ArcLLM's ``on_event`` callback through ``create_arcllm_bridge``
        so ``llm_call``, ``config_change``, and ``circuit_change`` events
        reach the ModuleBus (SPEC-017 R-001).
        """
        if self._model is None:
            from arcllm.trace_store import JSONLTraceStore

            agent_root = self._workspace.parent
            trace_store = JSONLTraceStore(agent_root)
            self._trace_store = trace_store
            # Bridge ArcLLM TraceRecords onto the ModuleBus. Bus is only
            # available after startup(); _ensure_model is lazy so this
            # holds in practice, but guard anyway.
            on_event = None
            if self._bus is not None:
                on_event = create_arcllm_bridge(self._bus)
            self._model = load_eval_model(
                self._config.llm.model,
                trace_store=trace_store,
                agent_label=self._config.agent.name,
                on_event=on_event,
            )
        return self._model

    async def _build_run_context(
        self, task: str
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
        telemetry, tool_registry, context, bus = self._ensure_started()
        model = self._ensure_model()

        tools = tool_registry.to_arcrun_tools()

        # Strategy prompt guidance — arcrun-owned strategies and tools.
        tool_names = [t.name for t in tools]
        strategy_sections = get_strategy_prompts(tool_names=tool_names)

        # Orchestration: register spawn_task if config enables it.
        # Closure mutation lets children inherit spawn_task — the tool's
        # closure captures ``tools`` by reference, so appending after
        # construction makes spawn visible to nested children too.
        if self._config.spawn.enabled:
            from arcagent.orchestration import SPAWN_GUIDANCE, make_spawn_tool

            # Children get the same orchestration guidance so nested
            # decomposition behaves consistently.
            child_system_prompt = await context.assemble_system_prompt(
                self._workspace,
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
                spawn_timeout_seconds=self._config.spawn.timeout_seconds,
                max_concurrent_spawns=self._config.spawn.max_concurrent,
            )
            tools.append(spawn_tool)
            strategy_sections = {**strategy_sections, "spawn_guidance": SPAWN_GUIDANCE}

        system_prompt = await context.assemble_system_prompt(
            self._workspace, extra_sections=strategy_sections
        )
        bridge = create_arcrun_bridge(
            bus,
            model_id=self._config.llm.model,
            agent_label=self._config.agent.name,
        )

        await bus.emit("agent:pre_respond", {"task": task})
        return telemetry, bus, model, tools, system_prompt, bridge

    async def _execute_loop(
        self,
        task: str,
        *,
        messages: list[Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Any:
        """Blocking execution: build tools, emit events, run loop."""
        telemetry, bus, model, tools, system_prompt, bridge = await self._build_run_context(task)
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
                    transform_context=self._context.transform_context if self._context else None,
                    tool_choice=tool_choice,
                )
        except Exception as exc:
            await bus.emit(
                "agent:error",
                {"task": task, "error": str(exc), "error_type": type(exc).__name__},
            )
            raise

        session_id = self._session.session_id if self._session else ""
        messages_dict = _build_messages_dict(task, result, messages)
        await bus.emit(
            "agent:post_respond",
            {"result": result, "messages": messages_dict, "session_id": session_id},
        )
        return result

    async def _execute_loop_async(
        self,
        task: str,
        *,
        messages: list[Message] | None = None,
        tool_choice: dict[str, Any] | None = None,
        session: SessionManager | None = None,
    ) -> AgentHandle:
        """Non-blocking execution: returns handle for steering.

        Telemetry span wrapping is caller-responsibility since the
        handle controls the execution lifetime.
        """
        telemetry, bus, model, tools, system_prompt, bridge = await self._build_run_context(task)
        context = self._context
        session_id = self._session.session_id if self._session else ""
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
        except Exception as exc:
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
        )

    async def run(self, task: str, *, tool_choice: dict[str, Any] | None = None) -> Any:
        """Execute a single task through the agent loop."""
        return await self._execute_loop(task, tool_choice=tool_choice)

    async def run_async(
        self, task: str, *, tool_choice: dict[str, Any] | None = None
    ) -> AgentHandle:
        """Execute a task, returning a handle for steering/cancellation."""
        return await self._execute_loop_async(task, tool_choice=tool_choice)

    async def _prepare_chat_session(self, message: str, session_id: str | None) -> SessionManager:
        """Ensure session exists and append the user turn.

        Raises RuntimeError if agent is not started.
        """
        if self._session is None:
            msg = "Agent not started. Call startup() first."
            raise RuntimeError(msg)
        session = self._session

        if session_id is not None:
            await session.resume_session(session_id)
        elif not session.session_id:
            await session.create_session()

        await session.append_message({"role": "user", "content": message})
        return session

    async def chat(self, message: str, *, session_id: str | None = None) -> Any:
        """Multi-turn conversation with persistent message history."""
        session = await self._prepare_chat_session(message, session_id)

        result = await self._execute_loop(
            message,
            messages=[Message(**m) for m in session.get_messages()],
        )

        response_text = getattr(result, "content", None) or ""
        await session.append_message({"role": "assistant", "content": response_text})

        # Check compaction threshold after each turn
        await self._maybe_compact(session)
        return result

    async def chat_async(self, message: str, *, session_id: str | None = None) -> AgentHandle:
        """Multi-turn conversation returning a steerable handle.

        Unlike chat(), result() automatically commits the assistant
        response to the session. Compaction is caller-responsibility.
        """
        session = await self._prepare_chat_session(message, session_id)

        return await self._execute_loop_async(
            message,
            messages=[Message(**m) for m in session.get_messages()],
            session=session,
        )

    async def chat_stream(
        self,
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

        Args:
            task: The user task or message.
            tool_choice: Optional tool-choice override forwarded to arcrun.

        Returns:
            An async iterator of :class:`~arcrun.streams.StreamEvent` objects.
        """
        _telemetry, tool_registry, context, bus = self._ensure_started()
        model = self._ensure_model()

        tools = tool_registry.to_arcrun_tools()
        tool_names = [t.name for t in tools]

        strategy_sections = get_strategy_prompts(tool_names=tool_names)

        if self._config.spawn.enabled:
            from arcagent.orchestration import SPAWN_GUIDANCE, make_spawn_tool

            child_system_prompt = await context.assemble_system_prompt(
                self._workspace,
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
                spawn_timeout_seconds=self._config.spawn.timeout_seconds,
                max_concurrent_spawns=self._config.spawn.max_concurrent,
            )
            tools.append(spawn_tool)
            strategy_sections = {**strategy_sections, "spawn_guidance": SPAWN_GUIDANCE}

        system_prompt = await context.assemble_system_prompt(
            self._workspace, extra_sections=strategy_sections
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
                    "session_id": self._session.session_id if self._session else "",
                },
            )

        return _wrapped_stream()

    async def _maybe_compact(self, session: SessionManager) -> None:
        """Trigger compaction if context ratio exceeds compact_threshold."""
        context = self._context
        if context is None:
            return
        ratio = session.token_ratio()
        if ratio >= self._config.context.compact_threshold:
            eval_model = self._ensure_model()
            await session.compact(eval_model, self._workspace)

    async def reload(self) -> str:
        """Re-scan capability roots; return R-005 diff string.

        Drops capability-loaded tools from the ToolRegistry, runs the
        loader's incremental scan (cached AST validation, drain-then-
        replace for background tasks, last-wins for tools/skills),
        re-bridges the new tool set, and re-subscribes hooks.
        """
        if not self._started:
            msg = "Agent not started. Call startup() first."
            raise RuntimeError(msg)

        async with self._reload_lock:
            loader = self._capability_loader
            registry = self._capability_registry
            tool_registry = self._tool_registry
            bus = self._bus
            if loader is None or registry is None or tool_registry is None or bus is None:
                return "reload: capability subsystem not initialized"

            # Drop capability-owned tools from ToolRegistry; the new
            # set is re-registered after scan.
            for name in self._capability_tool_names:
                tool_registry.unregister(name)
            self._capability_tool_names.clear()

            diff = await loader.scan_and_register()
            await self._bridge_capability_tools_to_registry()
            await self._bridge_capability_hooks_to_bus()
            await bus.emit("agent:tools_reloaded", {})
            text = diff.render()
            _logger.info("Reload complete: %s", text)
            return text

    @property
    def skills(self) -> list[SkillEntry]:
        """All registered skill entries."""
        if self._capability_registry is None:
            return []
        return list(self._capability_registry._skills.values())

    @property
    def settings(self) -> SettingsManager | None:
        """Runtime settings manager."""
        return self._settings

    async def shutdown(self) -> None:
        """Reverse-order teardown of all components.

        Closes the LLM model's httpx client before dropping the
        reference so connection pools are released deterministically
        (SPEC-017 R-004).
        """
        if not self._started:
            return

        bus = self._bus
        tool_registry = self._tool_registry
        if bus is None or tool_registry is None:
            return

        # Emit shutdown event
        await bus.emit("agent:shutdown", {})

        # Tear down capability lifecycles in reverse-topo order.
        if self._capability_loader is not None:
            await self._capability_loader.shutdown()

        # Reverse-order cleanup
        await bus.shutdown()
        await tool_registry.shutdown()

        # Close LLM client (releases httpx connection pool). Guarded
        # because _model is lazy — may never have been materialized.
        if self._model is not None:
            try:
                await self._model.close()
            except Exception:
                _logger.exception("Error closing LLM model on shutdown")
        self._model = None
        self._started = False
        _logger.info("Agent %s shut down", self._config.agent.name)

    async def _setup_capabilities(self, workspace: Path) -> None:
        """Wire the SPEC-021 capability subsystem.

        Builds the :class:`CapabilityRegistry`, configures per-module
        runtimes, scans builtin + enabled-module roots through the
        :class:`CapabilityLoader`, bridges discovered tools and hooks
        into the existing :class:`ToolRegistry` and :class:`ModuleBus`,
        and starts ``@capability`` class lifecycles.
        """
        bus = self._bus
        tool_registry = self._tool_registry
        telemetry = self._telemetry
        identity = self._identity
        if bus is None or tool_registry is None or telemetry is None or identity is None:
            msg = "Capability subsystem requires bus, tool_registry, telemetry, and identity"
            raise RuntimeError(msg)

        self._capability_registry = CapabilityRegistry(
            bus=bus,
            audit_sink=None,
            agent_did=identity.did,
            tier=self._config.security.tier,
        )

        # Configure builtin runtime — workspace + allowed_paths visible
        # to read/write/edit/bash; loader reference patched in below.
        from arcagent.builtins.capabilities import _runtime as builtin_runtime

        allowed_paths = [
            Path(p).resolve() for p in self._config.tools.policy.allowed_paths
        ] or None
        builtin_runtime.configure(
            workspace=workspace,
            allowed_paths=allowed_paths,
            loader=None,
            vault_resolver=self._vault_resolver,
        )

        # Configure each enabled module's runtime via signature dispatch.
        self._configure_module_runtimes(workspace)

        # Scan roots per SPEC-021 R-001 precedence:
        # 1. builtins + builtin skills (always)
        # 2. ~/.arc/capabilities/             — global, opt-in by user
        # 3. <agent_root>/capabilities/       — per-agent
        # 4. <workspace>/.capabilities/       — agent-authored
        # Plus enabled modules with capabilities.py.
        import arcagent.builtins.capabilities as builtins_pkg

        builtins_root = Path(builtins_pkg.__file__).parent
        scan_roots: list[tuple[str, Path]] = [
            ("builtins", builtins_root),
            ("builtins-skills", builtins_root / "skills"),
        ]

        global_root = Path("~/.arc/capabilities").expanduser()
        if global_root.is_dir():
            scan_roots.append(("global", global_root))

        agent_root = self._config_path.parent.resolve()
        agent_caps = agent_root / "capabilities"
        if agent_caps.is_dir():
            scan_roots.append(("agent", agent_caps))

        workspace_caps = workspace / ".capabilities"
        if workspace_caps.is_dir():
            scan_roots.append(("workspace", workspace_caps))

        modules_dir = Path(__file__).parent.parent / "modules"
        for mod_name, mod_entry in self._config.modules.items():
            if not mod_entry.enabled:
                continue
            mod_dir = modules_dir / mod_name
            if (mod_dir / "capabilities.py").is_file():
                scan_roots.append((f"module:{mod_name}", mod_dir))

        self._capability_loader = CapabilityLoader(
            scan_roots=scan_roots,
            registry=self._capability_registry,
            bus=bus,
        )
        builtin_runtime.configure(
            workspace=workspace,
            allowed_paths=allowed_paths,
            loader=self._capability_loader,
            vault_resolver=self._vault_resolver,
        )

        diff = await self._capability_loader.scan_and_register()
        if diff.errors:
            for path, detail in diff.errors:
                _logger.warning("Capability load error %s: %s", path, detail)
        _logger.info("Capability scan: %s", diff.render())

        await self._bridge_capability_tools_to_registry()
        await self._bridge_capability_hooks_to_bus()
        self._setup_capability_prompt_injection()
        await self._capability_loader.start_lifecycles()

    def _configure_module_runtimes(self, workspace: Path) -> None:
        """Call ``_runtime.configure(...)`` on every enabled module.

        Each module's configure() declares the kwargs it needs; we
        introspect the signature and pass only matching values.
        Modules without a ``_runtime`` submodule are silently ignored
        — they may legitimately have no shared state.
        """
        identity = self._identity
        telemetry = self._telemetry
        agent_name = self._config.agent.name
        team_root = self._config.team.root
        llm_config = self._config.llm
        eval_config = self._config.eval

        for mod_name, mod_entry in self._config.modules.items():
            if not mod_entry.enabled:
                continue
            try:
                runtime_mod = importlib.import_module(f"arcagent.modules.{mod_name}._runtime")
            except ImportError:
                continue
            configure_fn = getattr(runtime_mod, "configure", None)
            if configure_fn is None:
                continue

            available: dict[str, Any] = {
                "config": mod_entry.config,
                "eval_config": eval_config,
                "telemetry": telemetry,
                "workspace": workspace,
                "llm_config": llm_config,
                "agent_name": agent_name,
                "team_root": team_root,
                "bus": self._bus,
                "agent_did": identity.did if identity else "",
                "identity": identity,
            }
            sig = inspect.signature(configure_fn)
            kwargs = {name: value for name, value in available.items() if name in sig.parameters}
            try:
                configure_fn(**kwargs)
            except Exception:
                _logger.exception("Module %s _runtime.configure failed", mod_name)

    async def _bridge_capability_tools_to_registry(self) -> None:
        """Register every CapabilityRegistry tool into ToolRegistry.

        ToolRegistry owns the security wrapping (policy pipeline,
        audit, pre/post bus events, telemetry span). Capability tools
        flow through the same wrapper so behavior is identical.
        """
        registry = self._capability_registry
        tool_registry = self._tool_registry
        if registry is None or tool_registry is None:
            return
        async with registry._lock.reader:
            entries = list(registry._tools.values())
        for entry in entries:
            if entry.meta.name in self._capability_tool_names:
                continue
            registered = RegisteredTool(
                name=entry.meta.name,
                description=entry.meta.description,
                input_schema=entry.meta.input_schema,
                transport=ToolTransport.NATIVE,
                execute=entry.execute,
                source=str(entry.source_path),
            )
            tool_registry.register(registered)
            self._capability_tool_names.add(entry.meta.name)

    async def _bridge_capability_hooks_to_bus(self) -> None:
        """Subscribe each registered hook to the module bus.

        Idempotent: tracks already-bridged (event, name) pairs so
        reload doesn't double-subscribe.
        """
        registry = self._capability_registry
        bus = self._bus
        if registry is None or bus is None:
            return
        async with registry._lock.reader:
            hook_lists = {evt: list(hooks) for evt, hooks in registry._hooks.items()}
        for event, hooks in hook_lists.items():
            for hook in hooks:
                module_name = f"capability:{hook.meta.name}"
                if bus.handler_count_by_module(event, module_name) > 0:
                    continue
                bus.subscribe(
                    event=event,
                    handler=hook.handler,
                    priority=hook.meta.priority,
                    module_name=module_name,
                )

    def _setup_capability_prompt_injection(self) -> None:
        """Subscribe to agent:assemble_prompt to inject the capability manifest.

        Single subscriber at priority 85 calls
        :meth:`CapabilityRegistry.format_for_prompt` for the unified
        XML manifest (tools + skills). A second subscriber at priority
        91 injects the per-skill usage instruction.
        """
        bus = self._bus
        registry = self._capability_registry
        telemetry = self._telemetry
        if bus is None or registry is None:
            return

        async def _inject_capabilities(ctx: EventContext) -> None:
            sections = ctx.data.get("sections")
            if not isinstance(sections, dict):
                return
            prompt_text = await registry.format_for_prompt()
            if prompt_text:
                sections["capabilities"] = prompt_text
                if telemetry is not None:
                    telemetry.audit_event(
                        "prompt.capabilities_manifest_rebuilt",
                        {
                            "tool_count": len(registry._tools),
                            "skill_count": len(registry._skills),
                        },
                    )

        async def _inject_skill_usage(ctx: EventContext) -> None:
            sections = ctx.data.get("sections")
            if not isinstance(sections, dict) or not registry._skills:
                return
            sections["skill_usage"] = _SKILL_USAGE_INSTRUCTION

        bus.subscribe(
            event="agent:assemble_prompt",
            handler=_inject_capabilities,
            priority=85,
            module_name="capability_registry",
        )
        bus.subscribe(
            event="agent:assemble_prompt",
            handler=_inject_skill_usage,
            priority=91,
            module_name="capability_registry.skills",
        )

    def _create_vault_resolver(self) -> Any:
        """Create vault resolver from config.

        Validates the backend reference format before importing.
        Returns the instantiated vault backend.
        """
        backend_ref = self._config.vault.backend
        if not backend_ref:
            return None

        _validate_vault_backend(backend_ref)

        try:
            module_path, class_name = backend_ref.rsplit(":", 1)
            module = importlib.import_module(module_path)
            backend_cls = getattr(module, class_name)
            return backend_cls(cache_ttl_seconds=self._config.vault.cache_ttl_seconds)
        except Exception:
            _logger.exception("Failed to create vault resolver: %s", backend_ref)
            raise
