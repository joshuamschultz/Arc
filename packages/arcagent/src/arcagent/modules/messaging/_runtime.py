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
builds the dependency-free in-memory backend eagerly and defers any live NATS
connection to :func:`ensure_live_backend`, awaited once at poll-loop start.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.messaging import _bootstrap
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
    # Deliver a policy-gated teammate message into the agent's current run
    # (REQ-040/041); bound from the agent:ready payload alongside agent_run_fn.
    deliver_fn: Any = None
    # Whether the live NATS backend upgrade has run (idempotent guard).
    live_backend_ready: bool = False
    # Latest unread counts per stream — updated by the poll loop and read
    # by the assemble_prompt hook for context injection.
    last_unread: dict[str, int] = field(default_factory=dict)
    # agent.run_collected() callback — bound via agent:ready event.
    agent_run_fn: Any = None
    # Serialises message processing so only one inbox batch is in-flight.
    processing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # TTL-cached team roster string; invalidated after roster_ttl_seconds.
    roster_cache: str | None = None
    roster_cache_time: float = 0.0


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
) -> None:
    """Bind module state for the CURRENT asyncio task and bootstrap arcteam services.

    Called once at agent startup. Imports arcteam lazily so the module
    can be imported without arcteam installed (it is an optional dep).
    Builds the in-memory backend synchronously; a configured ``nats_url``
    is connected lazily by :func:`ensure_live_backend`.

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

    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry
    from arcteam.storage import MemoryBackend

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

    backend = MemoryBackend()
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
        signer=_bootstrap.message_signer(identity),
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
            svc=svc,
            registry=registry,
        )
    )


async def ensure_live_backend() -> None:
    """Upgrade to the live NATS JetStream backend when a url is configured.

    Idempotent: runs at most once. Rebuilds the audit chain, registry, and
    messenger over the shared, push-capable substrate so a served agent joins
    the real bus (REQ-020). With no ``nats_url`` this is a no-op and the
    in-memory backend built by :func:`configure` stays in place.
    """
    st = state()
    if st.live_backend_ready or not st.config.nats_url:
        st.live_backend_ready = True
        return

    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry
    from arcteam.storage import MemoryBackend

    # make_backend degrades an unreachable NATS to an in-memory backend (with a
    # single warning) rather than raising. When it did, keep the in-memory
    # services built by configure() instead of rebuilding over a fresh backend.
    backend = await _bootstrap.make_backend(st.config.nats_url)
    if isinstance(backend, MemoryBackend):
        st.live_backend_ready = True
        return

    audit = AuditLogger(backend, st.operator_signer)
    await audit.initialize()
    st.registry = EntityRegistry(backend, audit)
    st.svc = MessagingService(
        backend,
        st.registry,
        audit,
        signer=_bootstrap.message_signer(st.identity),
    )
    st.live_backend_ready = True
    _logger.info("Messaging upgraded to live NATS backend at %s", st.config.nats_url)


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "messaging module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["configure", "ensure_live_backend", "reset", "state"]
