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
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import arcrun
from arctrust import (
    AgentIdentity,
    FileNotaryTransit,
    OperatorKey,
    RecordCipher,
    Signer,
    WitnessAnchor,
    WormSink,
    parse_classification,
    worm_policy_sink,
)

from arcagent.capabilities.capability_registry import SkillEntry
from arcagent.core import agent_security
from arcagent.core.agent_dispatch import dispatch_stream
from arcagent.core.agent_lifecycle import setup_capabilities
from arcagent.core.background_tasks import BackgroundTaskSupervisor
from arcagent.core.config import ArcAgentConfig
from arcagent.core.model_manager import (
    create_arcllm_bridge,
    create_arcrun_bridge,
    ensure_model,
)
from arcagent.core.module_bus import ModuleBus
from arcagent.core.runtime_dependencies import RuntimeBinding
from arcagent.core.session_coordination import SessionRunCoordinator
from arcagent.core.session_internal import ContextManager, SessionManager
from arcagent.core.session_internal.capability_ledger import (
    LETHAL_TRIFECTA,
    SessionCapabilityLedger,
)
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_policy import build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry
from arcagent.core.vault_resolver import _validate_vault_backend, create_vault_resolver
from arcagent.tools._policy_fill import resolve_provider_limits
from arcagent.tools.human_gate import ApprovalChannel, HumanGate, HumanGateConfig

if TYPE_CHECKING:
    from arcagent.core.tool_policy import PolicyPipeline


_logger = logging.getLogger("arcagent.agent")

# key_ref the operator seed is stored under in a vault_transit keystore/HSM
# (mirrors arctrust.operator's vault operator id). SPEC-037 REQ-006.
_OPERATOR_KEY_REF = "operator"
_SHUTDOWN_STEP_TIMEOUT_SECONDS = 5.0


class _LifecycleState(Enum):
    """Serialized lifecycle phases for one agent instance."""

    STOPPED = "stopped"
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"


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
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_state = _LifecycleState.STOPPED
        self._started = False

        # Components initialized during startup()
        self._telemetry: AgentTelemetry | None = None
        self._identity: AgentIdentity | None = None
        # SPEC-053 — the deployment operator key (audit authority). Loaded
        # read-only at startup from OUTSIDE the workspace tool-sandbox; signs
        # every WORM chain so the audited agent is never its own audit
        # authority. Distinct from _identity (which attests ToolCalls only).
        self._operator_key: OperatorKey | None = None
        # SPEC-037 — the operator key resolved through the arctrust Signer seam.
        # Every WORM/checkpoint signature goes through this, so a federal
        # vault-transit deployment signs by reference (seed never in-process).
        self._operator_signer: Signer | None = None
        # D-577 — seals the captured content of every WORM record at rest. Full
        # connector capture (D-552) put email bodies and documents in the chain.
        self._record_cipher: RecordCipher | None = None
        # SPEC-053 — federal external witness for trace-checkpoint anchors. None
        # at personal/enterprise (tier = stringency: federal only ADDS this).
        self._witness: WitnessAnchor | None = None
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
        self._vault_resolver: Any = None
        # Overlay-aware prompt resolver, built once at capability setup and pinned
        # to the operator key (editable-system-prompts COMP-006). None until setup.
        self._prompt_resolver: Any = None
        self._model: Any = None
        self._trace_store: Any = None
        # Live steerable runs keyed by session (SPEC-031 D2). A tracked run
        # exists only while it executes; a teammate message arriving mid-run is
        # injected into it (steer/follow_up) instead of starting a new one.
        self._run_coordinator = SessionRunCoordinator()
        # Compatibility alias for existing operational/tests introspection.
        # Registration and identity-safe removal go through the coordinator.
        self._active_runs = self._run_coordinator.active_runs
        self._run_finalizers: set[asyncio.Task[None]] = set()
        self._background_tasks = BackgroundTaskSupervisor(logger=_logger)
        # Channel-delivery callback ("platform:chat_id", text) -> None. Injected
        # by the embedded gateway (which owns channels) before startup(); None
        # standalone. Surfaced to modules (scheduler) in the agent:ready payload
        # so a fired schedule's output can reach a channel. arcagent stays
        # string-only — the gateway parses the target (ADR: no arcgateway import).
        self._channel_deliver_fn: Callable[[str, str], Awaitable[Any]] | None = None
        # The arctrust policy pipeline (built in startup) — reused to authorize
        # mid-turn steering (REQ-041), the only steering caller in the system.
        self._policy_pipeline: PolicyPipeline | None = None
        # SPEC-035 — per-session lethal-trifecta ledger + human-approval gate.
        self._capability_ledger: SessionCapabilityLedger | None = None
        self._human_gate: HumanGate | None = None
        # Durable WORM sink for policy-decision audit records (SPEC-034). Holds
        # an exclusive lock for its lifetime; closed in shutdown().
        self._policy_worm: WormSink | None = None
        # Names of tools currently registered in ToolRegistry that came
        # from the capability loader. Tracked so reload() can drop them
        # cleanly and re-register the latest set.
        self._capability_tool_names: set[str] = set()
        # Task 27 follow-up (hotfix) — every ``_runtime.bind`` built during
        # startup, paired with the already-built state to rebind. A turn
        # dispatched in a fresh sibling asyncio.Task (SessionRouter spawns
        # one per turn; a cached agent's SECOND+ turn never re-runs
        # startup()) would otherwise see none of this agent's ContextVar
        # state. ``agent_lifecycle.activate_runtime_bindings`` replays this
        # list at the top of every turn-dispatch entry point.
        self._runtime_bindings: list[RuntimeBinding[Any]] = []

    def _policy_audit_log_path(self) -> Path:
        """Resolve the WORM chain file for policy-decision audit (SPEC-034).

        Uses ``config.security.policy_audit_log`` when set (relative paths
        resolve against the workspace); otherwise defaults into the SHARED
        arcstore worm dir (``<data_dir>/worm/audit-chain-<agent>.jsonl``) so the
        ingest that feeds the arcui Security screen actually tails it. The
        per-agent filename is required because a ``WormSink`` holds an exclusive
        flock for its lifetime — a fleet cannot share one active chain file.
        Falls back to the workspace when arcstore is not installed.
        """
        return agent_security.policy_audit_log_path(self)

    def _operator_key_path(self) -> Path:
        """Resolve the operator-key file (SPEC-053 REQ-004).

        Lives under ``security.operator_key_dir`` (default ``~/.arc/operator``),
        outside the workspace tool-sandbox so agent-invoked file tools cannot
        write or replace it.
        """
        return agent_security.operator_key_path(self)

    def _resolve_operator_signer(self, sec: Any) -> Signer:
        """Resolve the operator audit/approval signer from custody config (F1).

        Federal FIPS floor (SC-13/IA-7) is asserted first: fail closed before any
        signing key is used if the backend/algorithm are not FIPS-approved.

        - ``in_process``: load the read-only on-disk (or vault-resolved) operator
          seed and sign in-process (personal default).
        - ``vault_transit``: sign BY REFERENCE through a transit boundary; the
          seed never enters this process. ``self._operator_key`` stays ``None``.
          A transit that cannot be resolved fails closed — never a silent
          in-process fallback (NFR-3).
        """
        return agent_security.resolve_operator_signer(self, sec)

    def _resolve_record_cipher(self) -> RecordCipher | None:
        """Resolve the at-rest seal for WORM records (D-577).

        The key is derived from the operator seed this deployment already
        custodies, so encryption introduces no second secret to store or lose.
        Under ``vault_transit`` custody the seed never enters this process, so
        no at-rest key is resolvable here and records are written in the clear —
        which is exactly the key-custody question SPEC-063 owns. Degrade LOUD:
        a silent fall back to plaintext on the tier that asked for the strictest
        custody would be the worst possible failure of this control.
        """
        return agent_security.resolve_record_cipher(self)

    def _resolve_transit(self, sec: Any) -> FileNotaryTransit:
        """Resolve the out-of-process signing transit for vault_transit custody.

        Defaults to the reference ``FileNotaryTransit`` (dev/CI without an HSM);
        a real deployment swaps this seam for a Vault Transit / PKCS#11 adapter.
        Fails closed if the transit cannot serve the operator key — the composite
        must never degrade to in-process signing.
        """
        return agent_security.resolve_transit(self, sec)

    def _build_witness(self) -> WitnessAnchor | None:
        """Build the external witness for trace-checkpoint anchors (federal only).

        Tier is stringency, not a gate (ADR-019): operator-key separation holds
        at every tier; federal ADDS the witness. The medium lives at
        ``security.witness_medium_path`` — OUTSIDE ``operator_key_dir`` so the
        operator-key holder does not also own the witness (that would make the
        rollback check illusory). The air-gapped append-only medium is the
        Must-have path; the online transparency-log submitter
        (``arctrust.TransparencyLogWitness``) needs a network transport supplied
        by the deployment (SPEC-037) and is not wired here.
        """
        return agent_security.build_witness(self)

    def _trace_checkpoint_chain_path(self) -> Path:
        """Resolve the operator-signed trace-checkpoint WORM chain (SPEC-053)."""
        return agent_security.trace_checkpoint_chain_path(self)

    def _prior_audit_chains_exist(self) -> bool:
        """True if any WORM audit chain already exists for this deployment.

        A chain proves an operator key was previously present and signed it, so
        a now-missing key file is covert erasure, not a first-ever bootstrap —
        the operator-key load must fail closed rather than regenerate (SPEC-053).
        """
        return agent_security.prior_audit_chains_exist(self)

    def _verify_witness_consistency(self) -> None:
        """Fail closed at federal if the local head is not externally witnessed.

        Wires ``verify_inclusion`` into startup (SPEC-053 REQ-009): the newest
        verified local operator-signed anchor must appear in the separately
        custodied witness. If it does not — a rollback + re-anchor by a holder of
        the operator key, or a missing/unavailable witness — federal fails
        closed; other tiers warn. A deployment with nothing anchored yet passes.
        """
        agent_security.verify_witness_consistency(self)

    async def startup(self) -> None:
        """Initialize all components; release partial state on any failure.

        Wraps :meth:`_startup_impl`. ``_startup_impl`` opens the single-writer
        WORM audit lock (SPEC-034) early, then configures modules. A module that
        fails to configure (e.g. the browser module with no Chrome installed)
        must NOT leave that lock — or any started capability tasks — held for the
        process lifetime, or every later instance of this agent fails to acquire
        the lock (single-writer invariant). On failure we release what was
        acquired and re-raise the original error.
        """
        async with self._lifecycle_lock:
            if self._lifecycle_state is _LifecycleState.STARTED:
                return
            self._lifecycle_state = _LifecycleState.STARTING
            try:
                await self._startup_impl()
            except BaseException:
                await self._release_partial_startup()
                self._started = False
                self._lifecycle_state = _LifecycleState.STOPPED
                raise
            self._started = True
            self._lifecycle_state = _LifecycleState.STARTED

    async def _release_partial_startup(self) -> None:
        """Best-effort teardown of resources a failed :meth:`_startup_impl` acquired.

        Mirrors :meth:`shutdown`'s resource release but does NOT require
        ``self._started`` — a failure before that flag is set (which
        ``shutdown`` short-circuits on) is exactly the leak this guards against.
        """
        loader = self._capability_loader
        if loader is not None:
            with contextlib.suppress(Exception):
                await loader.shutdown()
        registry = self._tool_registry
        if registry is not None:
            with contextlib.suppress(Exception):
                await registry.shutdown()
        with contextlib.suppress(Exception):
            await self._background_tasks.drain()
        worm = self._policy_worm
        if worm is not None:
            with contextlib.suppress(Exception):
                worm.close()
            self._policy_worm = None
        self._runtime_bindings.clear()
        self._model = None

    async def _startup_impl(self) -> None:
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

        # 3.5 Operator signing authority (audit authority) — resolved by custody.
        # in_process loads the read-only on-disk seed; vault_transit signs by
        # reference and NEVER loads the seed into this process (SPEC-037 F1).
        sec = self._config.security
        self._operator_signer = self._resolve_operator_signer(sec)
        self._record_cipher = self._resolve_record_cipher()
        self._witness = self._build_witness()
        # Fail closed at federal if the local head diverged from the witness.
        self._verify_witness_consistency()

        # 4. Module Bus
        self._bus = ModuleBus()

        # 5. Tool Registry (with policy pipeline)
        # The agent admits its own identity: its DID -> pubkey seeds the
        # pipeline's IdentityLayer registry so its signed dispatches authenticate
        # (deny-by-default at enterprise/federal). Team peers are added when the
        # agent joins a team.
        tier = self._config.security.tier
        # Route every policy decision into a durable, Ed25519-signed WORM chain
        # (SPEC-034). arcagent owns the file path; arctrust owns the adapter and
        # the chain. Signed with the OPERATOR key (SPEC-053), never the agent
        # DID — the audited subject must not be its own audit authority.
        worm = WormSink(
            self._policy_audit_log_path(), self._operator_signer, cipher=self._record_cipher
        )
        self._policy_worm = worm
        policy_sink = worm_policy_sink(worm)
        # SPEC-035 REQ-011 — the lethal-trifecta forbidden composition is LIVE in
        # GlobalLayer at every tier. arcagent owns the deployment set; arctrust
        # receives the resolved frozenset.
        # SPEC-038 REQ-021 — bind the operator-declared clearance to identity.
        # Federal parses strict (unknown/empty label → fail closed).
        strict_classification = tier == "federal"
        self._identity.clearance = parse_classification(
            self._config.security.clearance, strict=strict_classification
        )
        pipeline = build_pipeline(
            tier=tier,  # type: ignore[arg-type]  # str vs Literal
            agent_registry={self._identity.did: self._identity.public_key},
            forbidden_compositions=[LETHAL_TRIFECTA],
            classification_enforced=self._config.security.classification_enforced,
            provider_limits=resolve_provider_limits(self._config),
            audit_sink=policy_sink,
        )
        self._policy_pipeline = pipeline
        # SPEC-035 REQ-012/014 — per-session capability ledger + human-approval
        # gate for trifecta completion. The gate verifies a one-shot approval
        # signed with the OPERATOR key (SPEC-053 authority), never the agent DID
        # (ASI09). The channel is the MECHANICAL, spoof-proof operator handoff:
        # the shared arcstore approvals directory the `arc approve` CLI and arcui
        # operator surface resolve — never agent chat, which could be forged.
        self._capability_ledger = SessionCapabilityLedger()
        gate_cfg = self._config.tools.human_gate
        human_gate = HumanGate(
            operator_signer=self._operator_signer,
            agent_did=self._identity.did,
            tier=tier,
            config=HumanGateConfig(
                timeout_seconds=gate_cfg.timeout_seconds,
                auto_approve=[frozenset(legs) for legs in gate_cfg.auto_approve],
                auto_approve_tools=frozenset(gate_cfg.auto_approve_tools),
            ),
            audit_sink=policy_sink,
            channel=self._build_approval_channel(gate_cfg.timeout_seconds),
        )
        self._human_gate = human_gate
        self._tool_registry = ToolRegistry(
            config=self._config.tools,
            bus=self._bus,
            telemetry=self._telemetry,
            policy_pipeline=pipeline,
            identity=self._identity,
            tier=tier,  # type: ignore[arg-type]  # str vs Literal
            capability_ledger=self._capability_ledger,
            human_gate=human_gate,
            provider_label=self._config.llm.model,
            resource_classifications=dict(self._config.tools.policy.classifications),
            classification_strict=strict_classification,
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

        # 8. Capability subsystem (replaces SkillRegistry, ExtensionLoader,
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
            {
                "run_fn": self.run_collected,
                "deliver_fn": self.deliver_message,
                "channel_deliver_fn": self._channel_deliver_fn,
                "classify_fn": self.quick_classify,
                "skill_registry": self._capability_registry,
                "capability_ledger": self._capability_ledger,
            },
        )

        await self._bus.emit("agent:init", {"config": self._config.agent.name})
        _logger.info(
            "Agent %s started (DID: %s)",
            self._config.agent.name,
            self._identity.did,
        )

    def _build_approval_channel(self, ttl_seconds: float) -> ApprovalChannel | None:
        """Mechanical operator handoff for the human-approval gate (SPEC-035).

        Lazy: opens the shared arcstore ``approvals`` directory only on the first
        trifecta block. Any construction error degrades to ``None`` (the gate then
        fails closed, exactly as with no channel) — approval must never crash boot.
        """
        try:
            from arcagent.tools.approval_channel import ArcStoreApprovalChannel
            from arcagent.tools.approval_store import open_approval_store
        except ImportError:  # arcstore not installed — fail closed, don't crash
            return None
        label = self._config.ui.display_name or self._config.agent.name
        return ArcStoreApprovalChannel(
            store_opener=open_approval_store,
            id_factory=lambda: uuid.uuid4().hex[:16],
            agent_label=label,
            ttl_seconds=ttl_seconds,
        )

    def _ensure_started(
        self,
    ) -> tuple[AgentTelemetry, ToolRegistry, ContextManager, ModuleBus]:
        """Validate agent is started and return narrowed component references."""
        if (
            self._lifecycle_state is not _LifecycleState.STARTED
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
                operator_signer=self._operator_signer,
                actor_did=self._identity.did if self._identity is not None else "",
                witness=self._witness,
                record_cipher=self._record_cipher,
                task_supervisor=self._background_tasks,
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
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        run_id: str | None = None,
        reply_target: str | None = None,
        reply_label: str | None = None,
        allowed_strategies: list[str] | None = None,
    ) -> AsyncIterator[arcrun.StreamEvent]:
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

        ``max_tokens`` / ``max_cost_usd`` are an optional per-run budget the
        caller pins (e.g. a planner step's slice of the plan aggregate). When
        set they tighten the config-resolved run budget (the lower ceiling
        wins) so a sub-task can never exceed either (SPEC-040 F2, LLM10).

        ``run_id`` optionally pins the run's correlation id (the task dispatcher
        passes it so a task links its run before the loop starts); when omitted
        arcrun mints one.

        ``allowed_strategies`` pins the loop's strategy allowlist for this turn
        (arcrun owns strategies; the agent only forwards the caller's choice).
        None leaves arcrun's own default selection in place.

        ``reply_target`` is the channel this turn arrived on ("platform:chat_id"),
        supplied by the gateway executor for interactive channel turns. It is
        recorded in the per-turn context so a capability like the scheduler can
        default a new schedule's delivery back to this channel, and remembered as
        a known channel (with ``reply_label``, a human-friendly name) so arcui can
        offer it in a delivery dropdown. None for non-channel runs
        (scheduler-driven, messaging inbox).
        """
        self._ensure_started()
        async for event in dispatch_stream(
            self,
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

    async def run_collected(
        self,
        input_text: str,
        *,
        session_key: str,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        run_id: str | None = None,
        allowed_strategies: list[str] | None = None,
    ) -> Any:
        """Run a turn on the ``session_key`` session and collect to a result.

        The single callback every non-streaming surface binds (scheduler,
        pulse, slack, telegram, messaging, planner steps): open-or-resume the
        keyed session, stream the turn, and return the final ``RunResult``.
        ``tool_choice`` is forwarded to the loop (see :meth:`run`).
        ``max_tokens`` / ``max_cost_usd`` pin a per-run budget the planner uses
        to slice a step off the plan aggregate (SPEC-040 F2) — the shared
        session (and its trifecta ledger) is preserved either way.
        """
        session = await self.session(session_key)
        return await arcrun.collect(
            self.run(
                input_text,
                session=session,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                max_cost_usd=max_cost_usd,
                run_id=run_id,
                allowed_strategies=allowed_strategies,
            )
        )

    def active_run(self, session_key: str) -> arcrun.RunHandle | None:
        """Return the live steerable run for ``session_key``, or None if idle."""
        return self._run_coordinator.active(session_key)

    async def start_tracked_run(
        self,
        input_text: str,
        *,
        session_key: str,
        reply_target: str | None = None,
        reply_label: str | None = None,
    ) -> arcrun.RunHandle:
        """Start an async, steerable run and track its handle under ``session_key``.

        The returned :class:`arcrun.RunHandle` lets a teammate message be
        injected mid-task (REQ-040/041). The handle is registered while the loop
        runs and removed by a finalizer that commits the assistant turn and
        compacts, matching the streaming path. ``reply_target`` / ``reply_label``
        name the channel the turn arrived on (see :meth:`run`).
        """
        from arcagent.core.agent_dispatch import start_tracked_run

        self._ensure_started()
        return await start_tracked_run(
            self,
            input_text,
            session_key=session_key,
            reply_target=reply_target,
            reply_label=reply_label,
        )

    async def quick_classify(self, *, system: str, user: str, max_tokens: int = 8) -> str:
        """Bounded single-shot classification for cheap gating decisions.

        One ``arcllm`` call, no tools, tiny token budget — NOT the agentic loop.
        A lightweight helper for module gates (e.g. the messaging channel-
        relevance triage) that must decide yes/no without paying a full run.
        Returns the model's stripped text. The caller interprets it.
        """
        self._ensure_started()
        model = self._ensure_model()
        response = await model.invoke(
            [
                arcrun.Message(role="system", content=system),
                arcrun.Message(role="user", content=user),
            ],
            max_tokens=max_tokens,
        )
        return (response.content or "").strip()

    def set_channel_deliver_fn(
        self,
        fn: Callable[[str, str], Awaitable[Any]] | None,
    ) -> None:
        """Inject the channel-delivery callback (embedded gateway wiring).

        Must be called BEFORE :meth:`startup` so the callback is present when
        ``agent:ready`` fires and modules (scheduler) bind it. Standalone
        deployments never call this — delivery stays disabled (None).
        """
        self._channel_deliver_fn = fn

    async def deliver_message(
        self,
        *,
        caller_did: str,
        message: str,
        session_key: str,
        interrupt: bool = False,
        reply_target: str | None = None,
        reply_label: str | None = None,
        parts: Sequence[Mapping[str, Any]] | None = None,
        on_handle: Callable[[arcrun.RunHandle], None] | None = None,
    ) -> str:
        """Deliver one inbound message — from a human surface or a teammate.

        The single entry every sender uses, so injection, session identity and
        audit behave identically whoever sent it (REQ-312). What happens to the
        message is the **agent's** call:

        * an interactive run is in flight → the message joins it (REQ-302), as a
          ``follow_up`` at the next turn boundary — the default for every sender
          that states nothing.
        * the session is idle, or its only run is background work such as a
          schedule or a consolidation pass → the message opens its own turn and
          the background run is left untouched (REQ-303).

        ``interrupt`` is a sender's *request* for a mid-turn steer, never a
        decision: it is honoured only when the arctrust policy pipeline also
        permits one for ``caller_did``, and a denied steer degrades to a
        ``follow_up`` (REQ-041). No human surface passes it — the gateway makes
        no run decision, and ``test_delivery_joins_live_turn`` fails if any
        surface starts. It exists for the teammate path, where the message
        itself carries the priority that earns an interrupt.

        The read and the run it may start are serialized per session, so two
        messages arriving in the same event-loop tick cannot both open a turn.
        ``on_handle`` receives the started run's handle, letting the caller stream
        that turn's result back to its channel; it is never called when the
        message joined a run already in flight, whose reply rides that run's own
        stream. ``reply_target`` / ``reply_label`` name the channel the message
        arrived on (see :meth:`run`).

        ``parts`` carries a message the sender composed out of more than words
        (SPEC-065 REQ-296/301): an ordered list in which an artefact is a
        *reference* into this agent's workspace. It is translated here, at the
        agent's own boundary, because this is the lowest layer allowed to know
        model types at all — ``arcgateway`` deliberately cannot. Senders with
        nothing but text pass ``message`` and never touch it.

        Returns the action taken: ``"steered"`` | ``"followed_up"`` | ``"started"``.
        """
        if parts:
            message = self._compose_from_parts(parts)
        self._ensure_started()
        async with self._run_coordinator.delivery(session_key):
            handle = self._run_coordinator.injection_target(session_key)
            if handle is None:
                started = await self.start_tracked_run(
                    message,
                    session_key=session_key,
                    reply_target=reply_target,
                    reply_label=reply_label,
                )
                if on_handle is not None:
                    on_handle(started)
                return "started"
            if interrupt and await self._authorize_steer(caller_did):
                await handle.steer(caller_did, message)
                return "steered"
            await handle.follow_up(caller_did, message)
            return "followed_up"

    def _compose_from_parts(self, parts: Sequence[Mapping[str, Any]]) -> str:
        """Render a multi-part message as the text this turn opens on.

        :class:`~arcagent.parts.PartTranslator` produces one block per part;
        for an artefact that block is a readable line naming the file, its type
        and where it is in the workspace — never its bytes. The agent can then
        open the file with its own tools if it needs the contents, and the
        session log keeps a reference that stays kilobytes forever (REQ-316).
        """
        from arcagent.parts import PartTranslator

        blocks = PartTranslator(workspace=self._workspace).to_history_content(parts)
        return "\n".join(str(block.get("text", "")) for block in blocks).strip()

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

            prepared = await loader.prepare_reload()
            diff = prepared.delta
            if diff.errors:
                # A reload is a transaction: an invalid candidate is useful
                # diagnostic output, never permission to damage the working set.
                rendered: str = diff.render()
                return rendered

            await loader.commit_reload(prepared)
            await bridge_capability_tools_to_registry(self)
            await bridge_capability_hooks_to_bus(self)
            await bus.emit("agent:tools_reloaded", {})
            text: str = diff.render()
            _logger.info("Reload complete: %s", text)
            return text

    @property
    def did(self) -> str:
        """This agent's DID — its cryptographic identity (empty before startup).

        Read-only accessor for out-of-process wiring (e.g. the always-on fleet
        registry keying an ArcAgent by identity) that must not reach into the
        private identity object.
        """
        return self._identity.did if self._identity is not None else ""

    @property
    def skills(self) -> list[SkillEntry]:
        """All registered skill entries."""
        if self._capability_registry is None:
            return []
        return list(self._capability_registry.skill_entries())

    @property
    def registered_tools(self) -> list[RegisteredTool]:
        """Every tool in the runtime ToolRegistry — the full wrapped surface.

        Builtins, capability-file tools, module tools, and self-authored tools,
        not a preset subset (REQ-095). Empty before startup. Read-only accessor
        for out-of-process observers (arcui capability views) that must not
        reach into the registry internals.
        """
        if self._tool_registry is None:
            return []
        return list(self._tool_registry.tools.values())

    async def shutdown(self) -> None:
        """Reverse-order teardown of all components.

        Closes the LLM model's httpx client before dropping the
        reference so connection pools are released deterministically
        (SPEC-017 R-004).
        """
        async with self._lifecycle_lock:
            if self._lifecycle_state is _LifecycleState.STOPPED:
                return
            self._lifecycle_state = _LifecycleState.STOPPING
            self._started = False  # reject new work before awaiting any teardown

            async def bounded(label: str, operation: Awaitable[Any]) -> None:
                try:
                    await asyncio.wait_for(operation, timeout=_SHUTDOWN_STEP_TIMEOUT_SECONDS)
                except TimeoutError:
                    _logger.error("Timed out while %s during shutdown", label)
                except BaseException:
                    _logger.exception("Error while %s during shutdown", label)

            try:
                bus = self._bus
                if bus is not None:
                    await bounded("emitting agent:shutdown", bus.emit("agent:shutdown", {}))

                # Stop accepting loop work, then request cancellation of every
                # run before waiting for its finalizer to commit/clean up.
                handles = list(self._active_runs.values())
                for handle in handles:
                    cancel = getattr(handle, "cancel", None)
                    if cancel is None:
                        _logger.warning("Active run has no cancellation operation")
                        continue
                    try:
                        operation = cancel(self.did, "agent shutdown")
                    except BaseException:
                        _logger.exception("Error requesting active-run cancellation")
                        continue
                    await bounded("cancelling an active run", operation)
                finalizers = list(self._run_finalizers)
                if finalizers:
                    await bounded(
                        "awaiting tracked-run finalizers",
                        asyncio.gather(*finalizers, return_exceptions=True),
                    )

                loader = self._capability_loader
                if loader is not None:
                    await bounded("stopping capabilities", loader.shutdown())
                await bounded("draining background tasks", self._background_tasks.drain())
                registry = self._tool_registry
                if registry is not None:
                    await bounded("stopping tools", registry.shutdown())

                worm = self._policy_worm
                if worm is not None:
                    try:
                        worm.close()
                    except BaseException:
                        _logger.exception("Error closing policy audit sink during shutdown")
                    finally:
                        self._policy_worm = None

                model = self._model
                if model is not None:
                    await bounded("closing LLM model", model.close())
                self._model = None
            finally:
                self._runtime_bindings.clear()
                self._run_coordinator.clear()
                self._run_finalizers.clear()
                self._sessions.clear()
                self._lifecycle_state = _LifecycleState.STOPPED
                _logger.info("Agent %s shut down", self._config.agent.name)
