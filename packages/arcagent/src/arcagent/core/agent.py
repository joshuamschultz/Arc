"""Agent orchestrator — wires all components, invokes ArcRun.

ArcAgent is the top-level class that owns all core components.
It initializes them in dependency order, bridges ArcRun events
to the Module Bus, and manages the full lifecycle.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcllm import Message
from arcrun import Event, RunHandle
from arcrun import run as arcrun_run
from arcrun import run_async as arcrun_run_async

from arcagent.core.config import ArcAgentConfig
from arcagent.core.context_manager import ContextManager
from arcagent.core.errors import ConfigError
from arcagent.core.extensions import ExtensionLoader
from arcagent.core.identity import AgentIdentity
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.session_manager import SessionManager
from arcagent.core.settings_manager import SettingsManager
from arcagent.core.skill_registry import SkillMeta, SkillRegistry
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import ToolRegistry
from arcagent.utils import load_eval_model

_logger = logging.getLogger("arcagent.agent")


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


def create_arcrun_bridge(bus: ModuleBus) -> Callable[[Event], None]:
    """Create on_event callback for arcrun.run().

    Maps ArcRun events to Module Bus events:
      tool.start  → agent:pre_tool
      tool.end    → agent:post_tool
      turn.start  → agent:pre_plan
      turn.end    → agent:post_plan
      llm.call    → (telemetry only, no bus event)

    ArcRun's on_event is synchronous (Callable[[Event], None]),
    so we schedule the async bus.emit via the running event loop.
    """
    _event_map = {
        "tool.start": "agent:pre_tool",
        "tool.end": "agent:post_tool",
        "turn.start": "agent:pre_plan",
        "turn.end": "agent:post_plan",
    }
    # Hold strong references to pending tasks so they aren't GC'd
    _pending: set[asyncio.Task[Any]] = set()

    def bridge(event: Event) -> None:
        bus_event = _event_map.get(event.type)
        if bus_event is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, event.data))
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
        self._extension_loader: ExtensionLoader | None = None
        self._skill_registry: SkillRegistry | None = None
        self._settings: SettingsManager | None = None
        self._vault_resolver: Any = None
        self._model: Any = None

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

        # 3. Identity
        self._identity = AgentIdentity.from_config(
            self._config.identity,
            vault_resolver=self._vault_resolver,
            org=self._config.agent.org,
            agent_type=self._config.agent.type,
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

        # Register built-in tools (read, write, edit, bash)
        workspace = self._workspace
        workspace.mkdir(parents=True, exist_ok=True)
        from arcagent.tools import create_builtin_tools

        # Wire allowed_paths from config to tools
        allowed_paths = [
            Path(p).resolve() for p in self._config.tools.policy.allowed_paths
        ] or None
        for tool in create_builtin_tools(workspace, allowed_paths=allowed_paths):
            self._tool_registry.register(tool)

        # Register user-configured native tools from config
        if self._config.tools.native:
            self._tool_registry.register_native_tools(self._config.tools.native)

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

        # 9. Skill Registry
        self._skill_registry = SkillRegistry()
        global_skills = Path("~/.arcagent/skills").expanduser()
        self._skill_registry.discover(workspace, global_skills)
        # Inject tools and skills into system prompt via bus events
        self._setup_tool_prompt_injection()
        self._setup_skill_prompt_injection()

        # 10. Extension Loader
        self._extension_loader = ExtensionLoader(
            tool_registry=self._tool_registry,
            bus=self._bus,
            telemetry=self._telemetry,
            config=self._config.extensions,
        )
        global_ext = Path(self._config.extensions.global_dir).expanduser()
        await self._extension_loader.discover_and_load(workspace, global_ext)

        # 11. Convention-based module loading (replaces _register_modules)
        module_ctx = ModuleContext(
            bus=self._bus,
            tool_registry=self._tool_registry,
            config=self._config,
            telemetry=self._telemetry,
            workspace=workspace,
            llm_config=self._config.llm,
        )
        self._load_modules_by_convention(module_ctx)

        # 12. Start modules with context
        await self._bus.startup(module_ctx)

        # Mark started BEFORE emitting agent:ready so modules that
        # immediately invoke agent.run() (e.g. scheduler) don't hit
        # the _ensure_started() guard.
        self._started = True

        # 13. Emit agent:ready with deferred-binding callbacks.
        # Modules subscribe to this event during startup() to receive
        # agent.run/chat callbacks without core knowing about them.
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
        await self._bus.emit("agent:extensions_loaded", {})
        await self._bus.emit("agent:skills_loaded", {})
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
        """Load and cache model on first use."""
        if self._model is None:
            self._model = load_eval_model(self._config.llm.model)
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
        Emits agent:pre_respond before returning.
        """
        telemetry, tool_registry, context, bus = self._ensure_started()
        model = self._ensure_model()

        tools = tool_registry.to_arcrun_tools()
        system_prompt = await context.assemble_system_prompt(self._workspace)
        bridge = create_arcrun_bridge(bus)

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

    async def _maybe_compact(self, session: SessionManager) -> None:
        """Trigger compaction if context ratio exceeds compact_threshold."""
        context = self._context
        if context is None:
            return
        ratio = session.token_ratio()
        if ratio >= self._config.context.compact_threshold:
            eval_model = self._ensure_model()
            await session.compact(eval_model, self._workspace)

    async def reload(self) -> None:
        """Re-discover extensions and skills. Hot reload.

        Clears extension-registered tools/hooks and skill cache,
        then re-discovers from all sources. Serialized via lock
        to prevent concurrent reloads from corrupting state.
        """
        if not self._started:
            msg = "Agent not started. Call startup() first."
            raise RuntimeError(msg)

        async with self._reload_lock:
            bus = self._bus
            tool_registry = self._tool_registry
            if bus is None or tool_registry is None:
                return

            # Clear extensions
            if self._extension_loader is not None:
                self._extension_loader.clear(tool_registry, bus)

            # Clear and re-discover skills
            if self._skill_registry is not None:
                self._skill_registry.clear()
                global_skills = Path("~/.arcagent/skills").expanduser()
                self._skill_registry.discover(self._workspace, global_skills)

            # Re-discover and load extensions
            if self._extension_loader is not None:
                global_ext = Path(self._config.extensions.global_dir).expanduser()
                await self._extension_loader.discover_and_load(self._workspace, global_ext)

            await bus.emit("agent:extensions_loaded", {})
            await bus.emit("agent:skills_loaded", {})
            await bus.emit("agent:tools_reloaded", {})
            _logger.info("Reload complete")

    @property
    def skills(self) -> list[SkillMeta]:
        """All discovered skills."""
        if self._skill_registry is None:
            return []
        return self._skill_registry.skills

    @property
    def settings(self) -> SettingsManager | None:
        """Runtime settings manager."""
        return self._settings

    async def shutdown(self) -> None:
        """Reverse-order teardown of all components."""
        if not self._started:
            return

        bus = self._bus
        tool_registry = self._tool_registry
        if bus is None or tool_registry is None:
            return

        # Emit shutdown event
        await bus.emit("agent:shutdown", {})

        # Clear extensions before bus shutdown
        if self._extension_loader is not None:
            self._extension_loader.clear(tool_registry, bus)

        # Reverse-order cleanup
        await bus.shutdown()
        await tool_registry.shutdown()

        # Clear skills
        if self._skill_registry is not None:
            self._skill_registry.clear()

        self._model = None
        self._started = False
        _logger.info("Agent %s shut down", self._config.agent.name)

    def _setup_tool_prompt_injection(self) -> None:
        """Subscribe to agent:assemble_prompt to inject tool catalog."""
        bus = self._bus
        tool_registry = self._tool_registry
        telemetry = self._telemetry
        if bus is None or tool_registry is None:
            return

        async def _inject_tools(ctx: Any) -> None:
            sections = ctx.data.get("sections")
            if not isinstance(sections, dict):
                return
            # Only audit on actual rebuild (cache miss)
            was_cached = tool_registry.is_prompt_cached
            prompt_text = tool_registry.format_for_prompt()
            if prompt_text:
                sections["tools"] = prompt_text
                if not was_cached and telemetry is not None:
                    telemetry.audit_event(
                        "prompt.tools_catalog_rebuilt",
                        {"tool_count": len(tool_registry.tools)},
                    )

        bus.subscribe(
            event="agent:assemble_prompt",
            handler=_inject_tools,
            priority=85,
            module_name="tool_registry",
        )

    def _setup_skill_prompt_injection(self) -> None:
        """Subscribe to agent:assemble_prompt to inject skill list."""
        bus = self._bus
        skill_registry = self._skill_registry
        if bus is None or skill_registry is None:
            return

        async def _inject_skills(ctx: Any) -> None:
            sections = ctx.data.get("sections")
            if not isinstance(sections, dict):
                return
            prompt_text = skill_registry.format_for_prompt()
            if prompt_text:
                sections["skills"] = prompt_text

        bus.subscribe(
            event="agent:assemble_prompt",
            handler=_inject_skills,
            priority=90,
            module_name="skill_registry",
        )

    def _load_modules_by_convention(self, ctx: ModuleContext) -> None:
        """Discover and register modules via convention-based loading.

        Scans arcagent/modules/*/MODULE.yaml for enabled modules,
        imports their entry_point classes, and registers with the bus.
        """
        bus = self._bus
        if bus is None:
            return

        from arcagent.core.module_loader import ModuleLoader

        loader = ModuleLoader()
        modules_dir = Path(__file__).parent.parent / "modules"
        loaded = loader.load_all(modules_dir, ctx)
        for mod in loaded:
            bus.register_module(mod)
            _logger.info("Registered module: %s", mod.name)

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
