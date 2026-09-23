"""Per-agent messaging module runtime context.

The messaging module's hooks, tools, and background polling task share
state (services, config, unread-count cache, agent run callback, etc.).
Decorator-stamped functions can't carry that state in a closure, so it
lives in a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
The poll loop (an ``@background_task``) is spawned via
``capability_registry.py``'s ``asyncio.create_task()`` AFTER ``configure()``
already ran in the same agent-startup task, so asyncio's automatic
context-copy on task creation gives it this agent's state for its whole
lifetime — no ``contextvars.copy_context()`` special-casing needed.

``configure`` is synchronous (called from the sync capability wiring), so it
builds a standalone in-memory backend only without a NATS URL. A configured
fleet starts unavailable and joins through :func:`ensure_live_backend`,
retried by the inbox loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.messaging.config import MessagingConfig

if TYPE_CHECKING:
    from arctrust import AgentIdentity

_logger = logging.getLogger("arcagent.modules.messaging._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across messaging hooks, tools, and poll task."""

    config: MessagingConfig
    workspace: Path
    telemetry: Any
    team_root: Path
    agent_name: str
    # The agent's arctrust identity — DID it registers under and key it signs
    # messages with (REQ-030). None only in verify-only/degraded setups.
    identity: AgentIdentity | None
    # The config-resolved OPERATOR signer (audit authority) — signs the messaging
    # WORM audit chain (SPEC-037 F4), never the agent DID seed. Same custody +
    # algorithm as the policy chain; under vault_transit it holds no seed.
    operator_signer: Any
    # arcteam service objects — set by configure(), typed as Any to avoid
    # a hard import-time dependency on the optional arcteam package.
    svc: Any  # MessagingService
    registry: Any  # EntityRegistry
    arcstore_opener: Any = None
    # Deliver a policy-gated teammate message into the agent's current run
    # (REQ-040/041); bound from the agent:ready payload alongside agent_run_fn.
    deliver_fn: Any = None
    _live_backend_started: bool = False
    _live_backend_failed: bool = False
    live_backend: Any = None
    live_subscription: Any = None
    # Latest unread counts per stream — updated by the poll loop and read
    # by the assemble_prompt hook for context injection.
    last_unread: dict[str, int] = field(default_factory=dict)
    # agent.run_collected() callback — bound via agent:ready event.
    agent_run_fn: Any = None
    # One bounded model call through ArcRun (agent.run_oneshot) — bound at
    # agent:ready. Breaks a tie the deterministic prefilter could not (ADR-032).
    oneshot_fn: Any = None
    # arcteam DigestStore — every agent's published index of what it holds. The
    # only artifact that crosses the memory privacy boundary, and what responder
    # selection routes over (ADR-032).
    digests: Any = None
    # Message ids the deferred sweep has already picked up. In-process, so a
    # restart may re-sweep a still-unanswered message once — which is the right
    # way round for a backstop whose failure to act is invisible.
    swept: set[str] = field(default_factory=set)
    # Channel delivery ("platform:chat_id", text) -> None from the embedded
    # gateway — bound at agent:ready. Powers ``notify_user`` (agent -> human).
    channel_deliver_fn: Any = None
    # Serialises message processing so only one inbox batch is in-flight.
    processing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # TTL-cached team roster string; invalidated after roster_ttl_seconds.
    roster_cache: str | None = None
    roster_cache_time: float = 0.0
    # SPEC-068 D1c — monotonic time this agent last woke for each channel, so
    # one agent cannot answer every message in a rapid exchange.
    channel_last_woken: dict[str, float] = field(default_factory=dict)
    # SPEC-068 D4d — per-channel breaker around the relevance gate.
    channel_breakers: dict[str, Any] = field(default_factory=dict)
    inbox_service: Any = None
    mail_service: Any = None
    inbox_backend: Any = None
    inbox_init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def live_backend_ready(self) -> bool:
        """Report active subscription and current backend availability."""
        if not self._live_backend_started or self._live_backend_failed:
            return False
        if not self.config.nats_url:
            return True
        return (
            self.live_subscription is not None
            and self.live_backend is not None
            and bool(self.live_backend.available)
        )

    @live_backend_ready.setter
    def live_backend_ready(self, ready: bool) -> None:
        self._live_backend_started = ready
        if ready:
            self._live_backend_failed = False


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_messaging_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    team_root: Path | None = None,
    agent_name: str = "",
    identity: AgentIdentity | None = None,
    operator_signer: Any = None,
    arcstore_opener: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task and bootstrap arcteam services.

    Called once at agent startup. Imports arcteam lazily so the module
    can be imported without arcteam installed (it is an optional dep).
    Builds standalone memory synchronously or a fail-closed unavailable backend;
    a configured ``nats_url`` is connected lazily by :func:`ensure_live_backend`.

    ``operator_signer`` (arctrust ``Signer``) signs the messaging WORM audit
    chain (SPEC-037 F4). It MUST be the deployment operator authority — never the
    agent DID seed, never an ephemeral key — so the audited subject is not its
    own audit authority (SPEC-053). Absent it, this fails closed rather than
    audit with a repudiable key.
    """
    if operator_signer is None:
        raise ValueError(
            "messaging module requires the operator signer to sign its audit "
            "chain (SPEC-037 F4) — refusing to fall back to the agent DID seed "
            "or an ephemeral key (fail-closed)"
        )

    from arcteam import MemoryBackend, UnavailableBackend
    from arcteam import composition as arcteam_composition
    from arcteam.audit import AuditLogger
    from arcteam.digest import DigestStore
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry

    # The scaffolded [modules.messaging] config omits entity_id/entity_name, but
    # the agent knows its own name and registration keys the inbox stream on the
    # handle (arc.agent.{name}). Default to that so the daemon subscribes to the
    # same stream peers send it — otherwise it listens on arc.agent.(empty) and
    # never receives anything.
    raw = dict(config or {})
    if not raw.get("entity_id") and agent_name:
        raw["entity_id"] = f"agent://{agent_name}"
    if not raw.get("entity_name") and agent_name:
        raw["entity_name"] = agent_name
    cfg = MessagingConfig(**raw)
    ws = workspace.resolve()
    resolved_team_root = (team_root or (ws.parent / "team")).resolve()

    backend = UnavailableBackend() if cfg.nats_url else MemoryBackend()
    audit = AuditLogger(backend, operator_signer)
    # AuditLogger.initialize() is async; callers that need it initialised
    # before the first poll must await it separately (the poll loop waits
    # 1 s before its first cycle, giving startup time to complete).
    registry = EntityRegistry(backend, audit)
    # Sign every outbound message with the agent's own key (REQ-030).
    svc = MessagingService(
        backend,
        registry,
        audit,
        signer=arcteam_composition.message_signer(identity),
    )

    _state_var.set(
        _State(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            team_root=resolved_team_root,
            agent_name=agent_name,
            identity=identity,
            operator_signer=operator_signer,
            arcstore_opener=arcstore_opener,
            svc=svc,
            registry=registry,
            digests=DigestStore(backend),
        )
    )


async def ensure_live_backend(handler: Any = None) -> Any:
    """Upgrade to the live NATS JetStream backend when a url is configured.

    Connect, initialize audit, and subscribe before exposing shared services.

    With no ``nats_url`` this is a no-op and the intentional in-memory backend
    built by :func:`configure` stays in place. A failed configured connection
    leaves the fail-closed unavailable services in place for a later retry.
    """
    st = state()
    if not st.config.nats_url:
        st.live_backend_ready = True
        return None
    if st.live_subscription is not None:
        return st.live_subscription
    if handler is None:
        raise ValueError("configured fleet subscription requires a message handler")

    from arcteam import FleetBackendUnavailableError
    from arcteam import composition as arcteam_composition
    from arcteam.audit import AuditLogger
    from arcteam.digest import DigestStore
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry

    identity = st.identity
    if identity is None:
        raise FleetBackendUnavailableError("fleet signing identity is unavailable")
    signer = arcteam_composition.message_signer(identity)
    if signer is None:
        raise FleetBackendUnavailableError("fleet signing identity is unavailable")
    entity_id = st.config.entity_id
    public_key = identity.public_key
    backend = await arcteam_composition.make_backend(st.config.nats_url)
    subscription = None
    activated = asyncio.Event()

    async def deliver_after_activation(message: Any) -> None:
        await activated.wait()
        await handler(message)

    try:
        audit = AuditLogger(backend, st.operator_signer)
        await audit.initialize()
        registry = EntityRegistry(backend, audit)
        svc = MessagingService(
            backend,
            registry,
            audit,
            signer=signer,
        )
        subscription = await svc.subscribe(entity_id, deliver_after_activation)
        if (
            st.identity is not identity
            or not identity.can_sign
            or identity.public_key != public_key
            or st.config.entity_id != entity_id
        ):
            raise FleetBackendUnavailableError("fleet identity changed during subscription")
    except BaseException:
        if subscription is not None:
            try:
                await subscription.stop()
            except Exception as exc:
                _logger.warning("failed fleet subscription cleanup: %s", type(exc).__name__)
        close = getattr(backend, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                _logger.warning("failed fleet connection cleanup: %s", type(exc).__name__)
        raise
    st.registry = registry
    st.svc = svc
    st.digests = DigestStore(backend)
    st.mail_service = None
    st.live_backend = backend
    st.live_subscription = subscription
    st.live_backend_ready = True
    activated.set()
    _logger.info("Messaging joined configured NATS fleet")
    return subscription


async def close_live_backend() -> None:
    """Stop fleet subscriptions and return configured messaging to unavailable."""
    st = state()
    st.live_backend_ready = False
    subscription, st.live_subscription = st.live_subscription, None
    backend, st.live_backend = st.live_backend, None
    if st.config.nats_url:
        from arcteam import UnavailableBackend
        from arcteam.audit import AuditLogger
        from arcteam.digest import DigestStore
        from arcteam.messenger import MessagingService
        from arcteam.registry import EntityRegistry

        unavailable = UnavailableBackend()
        audit = AuditLogger(unavailable, st.operator_signer)
        st.registry = EntityRegistry(unavailable, audit)
        st.svc = MessagingService(
            unavailable,
            st.registry,
            audit,
            signer=None,
        )
        st.digests = DigestStore(unavailable)
        st.mail_service = None
    try:
        if subscription is not None:
            await subscription.stop()
    finally:
        if backend is not None:
            close = getattr(backend, "close", None)
            if close is not None:
                await close()


async def ensure_durable_inbox() -> Any | None:
    """Open the shared Postgres inbox once for this agent process."""
    st = state()
    if st.inbox_service is not None:
        return st.inbox_service
    if st.arcstore_opener is None:
        return None
    async with st.inbox_init_lock:
        if st.inbox_service is not None:
            return st.inbox_service
        from arcstore.backends import PostgresInboxRepository
        from arcstore.inbox_projection import DurableInboxService

        backend = await st.arcstore_opener()
        st.inbox_backend = backend
        st.inbox_service = DurableInboxService(PostgresInboxRepository(backend))
        return st.inbox_service


async def ensure_agent_mail() -> Any:
    """Compose agent-originated mail over the atomic ArcStore/NATS seams."""
    st = state()
    if st.mail_service is not None:
        return st.mail_service
    inbox = await ensure_durable_inbox()
    if inbox is None or st.inbox_backend is None:
        raise RuntimeError("durable inbox delivery requires a storage backend")
    from arcstore.mail_outbox import PostgresMailOutbox
    from arcteam import AgentMailService, RegistryMailAddressBook, composition

    signer = composition.message_signer(st.identity)
    if signer is None:
        raise RuntimeError("durable inbox delivery requires the agent signing identity")
    st.mail_service = AgentMailService(
        st.svc,
        inbox,
        outbox=PostgresMailOutbox(st.inbox_backend),
        address_book=RegistryMailAddressBook(st.registry),
        signer=signer,
    )
    return st.mail_service


async def close_durable_inbox() -> None:
    """Release the module-owned ArcStore backend when the agent stops."""
    st = state()
    backend, st.inbox_backend = st.inbox_backend, None
    st.inbox_service = None
    st.mail_service = None
    if backend is not None:
        await backend.stop()


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "messaging module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call, no construction. Called at the top of
    every turn-dispatch entry point (task 27 follow-up hotfix) so a turn
    running in a fresh sibling ``asyncio.Task`` — not a descendant of the
    task that ran ``configure()`` — still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = [
    "bind",
    "close_durable_inbox",
    "close_live_backend",
    "configure",
    "ensure_durable_inbox",
    "ensure_live_backend",
    "reset",
    "state",
]
