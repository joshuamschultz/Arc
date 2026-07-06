"""Agent orchestrator — wires all components, invokes ArcRun.

ArcAgent is the top-level class that owns all core components.
It initializes them in dependency order, bridges ArcRun events
to the Module Bus, and manages the full lifecycle.

Sibling modules
---------------
- ``arcagent.core.agent_lifecycle``    — capability subsystem setup
  (``setup_capabilities`` and the bridge helpers it calls).
- ``arcagent.core.agent_dispatch``     — the single streaming ``run``
  body (``dispatch_stream``, ``build_run_context``, ``maybe_compact``).
- ``arcagent.core.vault_resolver``     — vault backend instantiation
  + reference validation.
- ``arcagent.core.model_manager``      — lazy model loader and the
  ArcRun/ArcLLM event bridges.

The bridge factories are re-exported through this module so existing
imports (``from arcagent.core.agent import create_arcrun_bridge,
create_arcllm_bridge``) keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcrun import collect
from arctrust import AgentIdentity, WormSink, worm_policy_sink

from arcagent.capabilities.capability_registry import SkillEntry
from arcagent.core.agent_dispatch import dispatch_stream
from arcagent.core.agent_lifecycle import setup_capabilities
from arcagent.core.config import ArcAgentConfig
from arcagent.core.model_manager import (
    create_arcllm_bridge,
    create_arcrun_bridge,
    ensure_model,
)
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal import ContextManager, SessionManager
from arcagent.core.settings_manager import SettingsManager
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_policy import build_pipeline
from arcagent.core.tool_registry import ToolRegistry
from arcagent.core.vault_resolver import _validate_vault_backend, create_vault_resolver

if TYPE_CHECKING:
    from arcrun import RunHandle, StreamEvent

    from arcagent.core.tool_policy import PolicyPipeline


_logger = logging.getLogger("arcagent.agent")


__all__ = [
    "ArcAgent",
    "_validate_vault_backend",
    "create_arcllm_bridge",
    "create_arcrun_bridge",
]


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
        # Keyed pool of sessions — one SessionManager per conversation
        # (a Slack thread, a UI tab, an agent-to-agent channel, a CLI key).
        # Different humans/agents talking to this agent are distinct,
        # concurrent sessions; turns through each still run sequentially
        # via arcrun. ``session(key)`` opens-or-resumes by key.
        self._sessions: dict[str, SessionManager] = {}
        self._sessions_lock = asyncio.Lock()
        self._capability_registry: Any = None
        self._capability_loader: Any = None
        self._settings: SettingsManager | None = None
        self._vault_resolver: Any = None
        self._model: Any = None
        self._trace_store: Any = None
        # Live steerable runs keyed by session (SPEC-031 D2). A tracked run
        # exists only while it executes; a teammate message arriving mid-run is
        # injected into it (steer/follow_up) instead of starting a new one.
        self._active_runs: dict[str, RunHandle] = {}
        self._run_finalizers: set[asyncio.Task[None]] = set()
        # The arctrust policy pipeline (built in startup) — reused to authorize
        # mid-turn steering (REQ-041), the only steering caller in the system.
        self._policy_pipeline: PolicyPipeline | None = None
        # Durable WORM sink for policy-decision audit records (SPEC-034). Holds
        # an exclusive lock for its lifetime; closed in shutdown().
        self._policy_worm: WormSink | None = None
        # Names of tools currently registered in ToolRegistry that came
        # from the capability loader. Tracked so reload() can drop them
        # cleanly and re-register the latest set.
        self._capability_tool_names: set[str] = set()

    def _policy_audit_log_path(self) -> Path:
        """Resolve the WORM chain file for policy-decision audit (SPEC-034).

        Uses ``config.security.policy_audit_log`` when set (relative paths
        resolve against the workspace); otherwise defaults to
        ``<workspace>/audit/policy-chain.jsonl``.
        """
        configured = self._config.security.policy_audit_log
        if configured:
            path = Path(configured)
            return path if path.is_absolute() else (self._workspace / path)
        return self._workspace / "audit" / "policy-chain.jsonl"

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
            self._vault_resolver = create_vault_resolver(self._config)

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

        # 5. Tool Registry (with policy pipeline)
        # The agent admits its own identity: its DID -> pubkey seeds the
        # pipeline's IdentityLayer registry so its signed dispatches authenticate
        # (deny-by-default at enterprise/federal). Team peers are added when the
        # agent joins a team.
        tier = self._config.security.tier
        # Route every policy decision into a durable, Ed25519-signed WORM chain
        # (SPEC-034). arcagent owns the file path + operator key; arctrust owns
        # the adapter and the chain. The agent signs records with its own DID
        # seed — the same key that authenticates its dispatches.
        worm = WormSink(self._policy_audit_log_path(), self._identity.signing_seed)
        self._policy_worm = worm
        pipeline = build_pipeline(
            tier=tier,  # type: ignore[arg-type]  # str vs Literal
            agent_registry={self._identity.did: self._identity.public_key},
            audit_sink=worm_policy_sink(worm),
        )
        self._policy_pipeline = pipeline
        self._tool_registry = ToolRegistry(
            config=self._config.tools,
            bus=self._bus,
            telemetry=self._telemetry,
            policy_pipeline=pipeline,
            identity=self._identity,
            tier=tier,  # type: ignore[arg-type]  # str vs Literal
        )

        workspace = self._workspace
        workspace.mkdir(parents=True, exist_ok=True)

        # 6. Context Manager
        self._context = ContextManager(
            config=self._config.context,
            telemetry=self._telemetry,
            bus=self._bus,
        )

        # 7. Session pool starts empty; managers are built on demand by
        # ``session(key)`` so concurrent conversations stay isolated.

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
        await setup_capabilities(self, workspace)

        # 10. Mark started BEFORE emitting agent:ready so capabilities that
        # immediately invoke agent.run() (e.g. scheduler) don't hit
        # the _ensure_started() guard.
        self._started = True

        # 11. Emit agent:ready with the single deferred-binding callback.
        # Every surface (scheduler, pulse, slack, telegram, messaging)
        # drives the agent the same way: one ``run_fn(input, *, session_key)``
        # that opens-or-resumes the keyed session, streams a turn, and
        # collects it to a final result.
        await self._bus.emit(
            "agent:ready",
            {"run_fn": self.run_collected, "deliver_fn": self.deliver_message},
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

        Wires ArcLLM's ``on_event`` callback through ``create_arcllm_bridge``
        so ``llm_call``, ``config_change``, and ``circuit_change`` events
        reach the ModuleBus (SPEC-017 R-001).
        """
        if self._model is None:
            model, trace_store = ensure_model(
                config=self._config,
                workspace=self._workspace,
                bus=self._bus,
            )
            self._model = model
            self._trace_store = trace_store
        return self._model

    async def session(self, key: str) -> SessionManager:
        """Open-or-resume the session for ``key`` from the agent's pool.

        Each distinct ``key`` (a channel id, a CLI key, an agent-to-agent
        thread) gets its own ``SessionManager`` with an isolated message log,
        cached for the agent's lifetime. Sessionless surfaces (CLI, scheduler)
        pass a deterministic key to get a stable local session.
        """
        self._ensure_started()
        # Guard get-or-create: open_or_resume awaits, so two concurrent callers
        # with the same key could otherwise both build a manager over the same
        # jsonl and clobber each other (split-brain history).
        async with self._sessions_lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            manager = SessionManager(
                config=self._config.session,
                context_config=self._config.context,
                telemetry=self._telemetry,
                workspace=self._workspace,
                context_manager=self._context,
            )
            await manager.open_or_resume(key)
            self._sessions[key] = manager
            return manager

    async def run(
        self,
        input_text: str,
        *,
        session: SessionManager,
        tool_choice: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Drive one agent turn. The only execution entry — always
        session-bound, always streaming.

        Appends the input to ``session``'s history, streams arcrun
        ``StreamEvent``s (token … turn-end), and commits the assistant
        turn on completion (history/audit parity with the old ``chat``).
        One-shot callers wrap this with ``collect()`` (or use
        ``run_collected``) for a final result.

        ``tool_choice`` is forwarded to arcrun's loop and applied on turn 0;
        pass ``{"type": "required"}`` from pipeline orchestrators that need
        the first turn to emit a tool call (typically a ``signals_completion``
        terminator).
        """
        self._ensure_started()
        async for event in dispatch_stream(
            self,
            input_text,
            session=session,
            tool_choice=tool_choice,
        ):
            yield event

    async def run_collected(
        self,
        input_text: str,
        *,
        session_key: str,
        tool_choice: dict[str, Any] | None = None,
    ) -> Any:
        """Run a turn on the ``session_key`` session and collect to a result.

        The single callback every non-streaming surface binds (scheduler,
        pulse, slack, telegram, messaging): open-or-resume the keyed session,
        stream the turn, and return the final ``RunResult``. ``tool_choice``
        is forwarded to the loop (see :meth:`run`).
        """
        session = await self.session(session_key)
        return await collect(self.run(input_text, session=session, tool_choice=tool_choice))

    def active_run(self, session_key: str) -> RunHandle | None:
        """Return the live steerable run for ``session_key``, or None if idle."""
        return self._active_runs.get(session_key)

    async def start_tracked_run(self, input_text: str, *, session_key: str) -> RunHandle:
        """Start an async, steerable run and track its handle under ``session_key``.

        The returned :class:`arcrun.RunHandle` lets a teammate message be
        injected mid-task (REQ-040/041). The handle is registered while the loop
        runs and removed by a finalizer that commits the assistant turn and
        compacts, matching the streaming path.
        """
        from arcagent.core.agent_dispatch import start_tracked_run

        return await start_tracked_run(self, input_text, session_key=session_key)

    async def deliver_message(
        self,
        *,
        caller_did: str,
        message: str,
        session_key: str,
        interrupt: bool,
    ) -> str:
        """Deliver a teammate message into the agent's run for ``session_key``.

        The single steering caller in the system (SDD C8). Default is
        ``follow_up`` at the next turn boundary (REQ-040); ``steer`` is used
        mid-turn only when ``interrupt`` is set AND the arctrust policy pipeline
        permits it for ``caller_did`` (REQ-041) — a denied steer degrades to
        ``follow_up`` rather than interrupting. With no active run for the
        session, a fresh tracked run is started instead. Returns the action
        taken: ``"steered"`` | ``"followed_up"`` | ``"started"``.
        """
        self._ensure_started()
        handle = self._active_runs.get(session_key)
        if handle is None:
            await self.start_tracked_run(message, session_key=session_key)
            return "started"
        if interrupt and await self._authorize_steer(caller_did):
            await handle.steer(caller_did, message)
            return "steered"
        await handle.follow_up(caller_did, message)
        return "followed_up"

    async def _authorize_steer(self, caller_did: str) -> bool:
        """Whether the policy pipeline permits a mid-turn steer for ``caller_did``.

        Fail-closed authorization (REQ-041): a steer is permitted ONLY on an
        explicit ALLOW from a present pipeline. The agent authorizes its own
        steer by signing a ``messaging_steer`` :class:`ToolCall` (carrying the
        triggering ``caller_did``) with its identity and running it through the
        arctrust pipeline. Every non-ALLOW outcome denies: a missing pipeline or
        identity, a raising pipeline, or an explicit DENY. Every denial is
        audited and degrades the delivery to ``follow_up`` rather than
        interrupting; the sender was already authenticated at the
        message-signature layer.
        """
        pipeline = self._policy_pipeline
        identity = self._identity
        if pipeline is None or identity is None:
            self._audit_steer_denied(caller_did, layer="none", rule_id="", reason="no pipeline")
            return False
        from arcagent.core.tool_policy import PolicyContext, ToolCall, sign_call

        call = ToolCall(
            tool_name="messaging_steer",
            arguments={"caller_did": caller_did},
            agent_did=identity.did,
            session_id="",
            classification="unclassified",
        )
        call = sign_call(call, identity)
        ctx = PolicyContext(
            tier=self._config.security.tier,  # type: ignore[arg-type]  # str vs Literal
            policy_version="v0",
            bundle_age_seconds=0.0,
        )
        try:
            decision = await pipeline.evaluate(call, ctx)
        except Exception as exc:  # reason: fail-closed — a broken pipeline denies
            self._audit_steer_denied(
                caller_did, layer="error", rule_id="", reason=f"evaluate raised: {exc}"
            )
            return False
        if decision.is_deny():
            self._audit_steer_denied(
                caller_did,
                layer=decision.layer,
                rule_id=decision.rule_id,
                reason=decision.reason,
            )
            return False
        return True

    def _audit_steer_denied(
        self,
        caller_did: str,
        *,
        layer: str | None,
        rule_id: str | None,
        reason: str | None,
    ) -> None:
        """Emit the ``messaging.steer.denied`` audit event for a blocked steer."""
        if self._telemetry is not None:
            self._telemetry.audit_event(
                "messaging.steer.denied",
                {
                    "caller_did": caller_did,
                    "layer": layer,
                    "rule_id": rule_id,
                    "reason": reason,
                },
            )

    async def reload(self) -> str:
        """Re-scan capability roots; return R-005 diff string.

        Drops capability-loaded tools from the ToolRegistry, runs the
        loader's incremental scan (cached AST validation, drain-then-
        replace for background tasks, last-wins for tools/skills),
        re-bridges the new tool set, and re-subscribes hooks.
        """
        from arcagent.core.agent_lifecycle import (
            bridge_capability_hooks_to_bus,
            bridge_capability_tools_to_registry,
        )

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
            await bridge_capability_tools_to_registry(self)
            await bridge_capability_hooks_to_bus(self)
            await bus.emit("agent:tools_reloaded", {})
            text: str = diff.render()
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

        # Release the WORM chain lock (SPEC-034).
        if self._policy_worm is not None:
            self._policy_worm.close()
            self._policy_worm = None

        # Close LLM client (releases httpx connection pool). Guarded
        # because _model is lazy — may never have been materialized.
        if self._model is not None:
            try:
                await self._model.close()
            except Exception:  # reason: fail-open — log + continue
                _logger.exception("Error closing LLM model on shutdown")
        self._model = None

        self._started = False
        _logger.info("Agent %s shut down", self._config.agent.name)
