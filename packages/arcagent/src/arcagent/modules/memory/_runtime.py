"""Per-agent runtime state for the thin memory (Brain) wiring.

The memory hooks/tool/background-task share the config-selected
:class:`~arcagent.brain.Brain`, the bus (for ACL gating), and small per-turn
bookkeeping (a once-per-turn recall cache; a capture counter + last-activity
clock that trigger consolidation). Decorator-stamped functions read this lazily
via :func:`state`.

Isolation model (SECURITY-CRITICAL — ASI03 Identity Abuse / LLM02 data
disclosure). A single process runs MANY agents concurrently (the embedded
gateway caches up to 32 distinct :class:`~arcagent.core.agent.ArcAgent`
instances). Memory holds one agent's PRIVATE recall; handing it to a different
agent's turn is a cross-agent private-data bleed into an LLM prompt.

Two mechanisms enforce shared-nothing isolation, and neither trusts the other:

* **DID-keyed registry** (root fix). ``configure``/:func:`bind` register each
  agent's :class:`_State` under its own ``agent_did`` in :data:`_registry`.
  There is no single global slot to clobber, so a later agent's ``configure``
  can never overwrite an earlier agent's state (last-writer-wins is gone).
* **Fail-closed resolution** (the security stop). :func:`state` resolves the
  state for the DID bound to the RUNNING turn (:data:`_current_did`), asserts
  the resolved state actually belongs to that DID, and REFUSES the read —
  raising :class:`MemoryIsolationError` and emitting an audit event — on any
  missing binding, missing registration, or DID mismatch. It never falls back
  to ambient state, so an un-rebound or mis-bound read fails closed instead of
  leaking another agent's Brain.

``_current_did`` is a :class:`contextvars.ContextVar`, so it is per-asyncio-task
and copied into child tasks at creation. ``configure`` binds it for the startup
task (background tasks spawned there — e.g. ``memory_consolidate_loop`` — copy
it and stay pinned to this agent for their whole lifetime). Every turn-dispatch
entry point rebinds it via ``activate_runtime_bindings`` because a turn runs in
a fresh SIBLING task that does not inherit the startup binding. Rebinding is now
a correctness convenience, not the sole thing preventing a leak — the fail-closed
resolution guarantees isolation even if a rebind is ever missed.

When the selected brain is a :class:`~arcagent.brain.NullBrain`, ``active`` is
``False`` and every hook short-circuits — memory is a truly silent no-op (no
events, no files).
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

from arcprompt import PromptSource, StockPromptSource

from arcagent.brain import Brain, NullBrain, select_brain
from arcagent.knowledge import KnowledgeAccess, PersonalKnowledgePort, SharedKnowledgePort
from arcagent.modules.memory.config import MemoryConfig

_logger = logging.getLogger("arcagent.modules.memory._runtime")

_RECALL_CACHE_CAP = 8


class MemoryIsolationError(RuntimeError):
    """Memory state could not be resolved to the running agent's DID.

    Raised fail-closed rather than return another agent's Brain — the severed
    cross-agent isolation check restored (ASI03 / LLM02). Subclasses
    ``RuntimeError`` so callers that already handle the "memory not configured"
    runtime error keep catching this too.

    MIRROR PAIR — keep in lockstep with ``arcmemory.isolation.MemoryIsolationError``
    and its ``memory.isolation_fault`` audit event. Same name, same ``RuntimeError``
    base, same audit action, but a SEPARATE type: arcmemory may not import arcagent
    (the DAG boundary), so the build-time guard (``arcmemory.isolation``, which vets
    ``build_brain``'s {workspace, agent_did, identity}) mirrors this runtime-resolution
    guard rather than sharing it. Evolve them together — a change to the error shape or
    the audit vocabulary here belongs there too.
    """


@dataclass
class _State:
    """Mutable runtime state shared across the memory hooks/tool/task."""

    config: MemoryConfig
    brain: Brain
    workspace: Path
    telemetry: Any
    bus: Any
    agent_did: str
    active: bool
    # Explicit curated knowledge remains local to this agent's workspace.
    knowledge_access: KnowledgeAccess | None = None
    personal_knowledge: PersonalKnowledgePort | None = None
    # Fleet shared-knowledge port, attached by the fleet after start (or None).
    shared_knowledge: SharedKnowledgePort | None = None
    # The runtime identity's access (DID + clearance). A promotion publisher built
    # for a later-attached port acts only as this, never as anything item content says.
    runtime_access: KnowledgeAccess | None = None
    # The agent's prompt lookup (overlay first, then stock) for this module's own
    # prompt sections; stock when the agent handed none down.
    prompt_source: PromptSource = field(default_factory=StockPromptSource)
    # Once-per-turn recall cache: query-hash -> injectable text (bounds the
    # spawn double-assembly to a single retrieve).
    recall_cache: dict[int, str] = field(default_factory=dict)
    # Proactive (detected-moment) recall text staged by the ``agent:moment``
    # subscriber, drained + merged into ``sections["recall"]`` at the next
    # prompt assembly. Session/turn-scoped, in-memory, rebuild-free.
    proactive_buffer: list[str] = field(default_factory=list)
    # Whether this process already seeded the routing digest from existing
    # holdings (backfill runs once per start; the digest self-dedups, but a flag
    # spares a few hundred needless bus emits on every agent:ready).
    digest_backfilled: bool = False
    # Consolidation trigger bookkeeping.
    events_since_consolidate: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    last_consolidate_at: float = field(default_factory=time.monotonic)
    # Whether the surface index has been warmed once since startup. Recall on a turn
    # is query-only (never embeds the corpus); the background poll keeps the index
    # fresh, and warms it once at startup so a cold/empty index becomes searchable
    # without waiting for the first capture.
    index_warmed: bool = False


# Per-agent state keyed by the owning agent's DID. Shared across the process's
# agents on purpose: it is the SET of every agent's state, never a single
# clobberable slot. Reads select one entry via the turn-bound DID below.
_registry: dict[str, _State] = {}

# The DID of the agent whose turn is running in THIS asyncio task. Bound at
# configure() (for the startup task + the background tasks it spawns) and at
# every turn-dispatch entry (activate_runtime_bindings). Empty outside a bound
# context, which makes state() fail closed rather than guess.
_current_did: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arcagent_memory_current_did", default=""
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    bus: Any = None,
    agent_did: str = "",
    agent_name: str = "",
    identity: Any = None,
    policy_pipeline: Any = None,
    audit_sink: Any = None,
    shared_knowledge: SharedKnowledgePort | None = None,
    tier: str = "",
    prompt_source: PromptSource | None = None,
) -> None:
    """Build this agent's Brain-backed state, register it, and bind its DID.

    Called once at agent startup. Registers under ``agent_did`` (never a single
    shared slot) and binds ``_current_did`` for the running startup task so the
    background tasks spawned there copy this agent's DID and stay pinned to it.
    ``identity`` (the agent's signer) and ``policy_pipeline`` are threaded to the
    Brain so the agentic consolidation engine's memory-tool writes are signed +
    policy-authorized.

    ``audit_sink`` is the agent's audit sink (telemetry plus ``write_durable`` into
    its signed WORM chain); the Brain audits through it, so the promotion egress
    record is durable.

    ``shared_knowledge`` is not a core dependency key, so core never supplies it;
    the fleet attaches its port after start through
    :meth:`~arcagent.core.agent.ArcAgent.attach_shared_knowledge`, which reaches
    :func:`attach_shared_knowledge` below. A reconfigure (module reload) keeps the
    port already attached for this DID. When promotion is enabled the Brain
    receives the promotion settings as a plain mapping (it builds its own
    classifier) and a :class:`SharedKnowledgePublisher` when a port is present.

    ``tier`` is the agent's runtime tier (``[security].tier``). When given, it IS
    the memory tier: the brain's stringency settings and the federal promotion
    lock both follow it, so a memory block that omits its tier still runs at the
    agent's real tier. See :func:`_memory_config`.

    ``prompt_source`` is the agent's overlay-aware prompt lookup (ADR-033). It is
    handed to the Brain (every arcmemory prompt resolves through it) and kept for
    this module's own prompt sections; ``None`` means stock prompts only.

    Raises:
        ValueError: the memory block names a tier that disagrees with ``tier``,
            or the resulting config fails its own validation (e.g. promotion
            enabled at federal).
    """
    cfg = _memory_config(config or {}, tier)
    ws = Path(workspace).resolve()
    access = KnowledgeAccess(agent_did, _clearance_name(identity))
    prior = _registry.get(agent_did)
    if shared_knowledge is None and prior is not None:
        shared_knowledge = prior.shared_knowledge
    promotion_config = cfg.promotion.model_dump() if cfg.promotion.enabled else None
    publisher = (
        _promotion_publisher(ws, access, shared_knowledge)
        if promotion_config is not None and shared_knowledge is not None
        else None
    )
    brain = select_brain(
        cfg.brain,
        workspace=ws,
        agent_did=agent_did,
        agent_name=agent_name,  # labels background memory jobs in the run list
        tier=cfg.tier,
        audit_sink=audit_sink,
        brain_allowlist=tuple(cfg.brain_allowlist),
        identity=identity,
        policy_pipeline=policy_pipeline,
        backend_config=dict(cfg.backend),
        promotion_config=promotion_config,
        promotion_publisher=publisher,
        prompt_source=prompt_source,
    )
    knowledge_access: KnowledgeAccess | None = None
    personal_knowledge: PersonalKnowledgePort | None = None
    if cfg.curated_knowledge_enabled:
        from arcmemory.adapters import PersonalKnowledgeAdapter

        knowledge_access = access
        personal_knowledge = cast(PersonalKnowledgePort, PersonalKnowledgeAdapter(ws, agent_did))
    new_state = _State(
        config=cfg,
        brain=brain,
        workspace=ws,
        telemetry=telemetry,
        bus=bus,
        agent_did=agent_did,
        active=not isinstance(brain, NullBrain),
        knowledge_access=knowledge_access,
        personal_knowledge=personal_knowledge,
        shared_knowledge=shared_knowledge,
        runtime_access=access,
        prompt_source=prompt_source if prompt_source is not None else StockPromptSource(),
    )
    _registry[agent_did] = new_state
    _current_did.set(agent_did)
    _logger.info("memory module configured (brain=%s, active=%s)", cfg.brain, new_state.active)


def _normalize_tier(tier: str) -> str:
    return tier.strip().casefold()


def _memory_config(config: dict[str, Any], agent_tier: str) -> MemoryConfig:
    """The memory config, pinned to the agent's deployment tier.

    With no agent tier (a bare module test harness) the block's own tier applies.
    A block tier that disagrees with the agent tier is refused, never used: a
    stale or copied block must not run a federal agent's memory at a looser tier.
    The tier is case-folded so the backend never sees a spelling it would fold
    to its personal default.
    """
    block_tier = config.get("tier")
    if not agent_tier:
        return MemoryConfig(**{**config, "tier": _normalize_tier(str(block_tier or "personal"))})
    tier = _normalize_tier(agent_tier)
    if block_tier is not None and _normalize_tier(str(block_tier)) != tier:
        raise ValueError(
            f"[modules.memory.config] tier {block_tier!r} disagrees with the agent's "
            f"deployment tier {tier!r}; remove it or set it to {tier!r}"
        )
    return MemoryConfig(**{**config, "tier": tier})


def _clearance_name(identity: Any) -> str:
    """The runtime identity's clearance label (``UNCLASSIFIED`` when it names none)."""
    clearance = getattr(identity, "clearance", "UNCLASSIFIED")
    return str(getattr(clearance, "name", clearance))


def _promotion_publisher(
    workspace: Path,
    access: KnowledgeAccess,
    port: SharedKnowledgePort,
) -> Any:
    """The shared-write publisher, acting only as this agent's runtime identity.

    Imported lazily: the publisher needs ``arcmemory``, which a NullBrain agent
    may not have installed.
    """
    from arcmemory.adapters.memory_export import ConsolidatedMemoryExporter

    from arcagent.modules.memory.promotion import SharedKnowledgePublisher

    exporter = ConsolidatedMemoryExporter.for_workspace(workspace, access.caller_did)
    return SharedKnowledgePublisher(
        port=port,
        exporter=cast(Any, exporter),
        access_factory=lambda: access,
    )


@runtime_checkable
class _PromotionBindable(Protocol):
    """A Brain whose promotion sweep can take a publisher after it was built."""

    def bind_promotion_publisher(self, publisher: object | None) -> None: ...


def attach_shared_knowledge(agent_did: str, port: SharedKnowledgePort | None) -> None:
    """Hold (or, with ``None``, drop) the fleet shared-knowledge port for one agent.

    Resolved by the NAMED DID (:func:`state_for`), so an attach delivered for one
    agent can never land on another agent's state; an unregistered DID fails closed.
    With promotion enabled, the live Brain's sweep is re-bound to a publisher over
    the new port (``None`` on detach, so the next sweep sends nothing). The Brain
    is never rebuilt.
    """
    st = state_for(agent_did)
    st.shared_knowledge = port
    if not st.config.promotion.enabled:
        return
    if not isinstance(st.brain, _PromotionBindable):
        _logger.warning("memory promotion enabled but the brain cannot take a publisher")
        return
    access = st.runtime_access
    publisher = (
        _promotion_publisher(st.workspace, access, port)
        if port is not None and access is not None
        else None
    )
    st.brain.bind_promotion_publisher(publisher)


def refuse_uncertified_shared_knowledge(agent_did: str, event: str) -> None:
    """Ignore and audit a shared-knowledge event the agent core did not emit.

    Any module can emit on the shared bus; only the core may hand memory a port
    (a forged port would capture every promoted card). The refusal is recorded on
    the named agent's telemetry when it is registered, else on any live sink.
    """
    _logger.warning("ignored %s not emitted by the agent core (agent=%r)", event, agent_did)
    detail = {"agent_did": agent_did, "event": event, "outcome": "deny"}
    st = _registry.get(agent_did)
    if st is not None and st.telemetry is not None:
        st.telemetry.audit_event("memory.shared_knowledge_forged", detail)
        return
    _emit_any_audit("memory.shared_knowledge_forged", detail)


def state() -> _State:
    """Return the running agent's memory state, resolved by its bound DID.

    Fail-closed on identity: the state is selected by the DID bound for the
    running turn, never by ambient last-writer-wins global. A missing binding,
    a missing registration, or a DID mismatch refuses the read (raises + audits)
    instead of handing back another agent's Brain.

    An empty registry is reported separately, as a plain "not configured" error.
    Nothing has been configured for ANY agent in this process, so there is no
    isolation question to answer — the module is simply not installed, which
    since SPEC-066 is the ordinary state of a box where no operator ran
    ``arc module install memory``. Folding that into the isolation branch named
    the wrong cause ("no agent DID bound" sends the reader after an identity
    bug) and emitted an ASI03 isolation-fault audit for a routine, benign state,
    which is noise on the one channel that exists to catch real cross-agent
    bleed.
    """
    if not _registry:
        raise RuntimeError(
            "memory state read before the memory module was configured; the module is "
            "not installed at the deployment module root, or [modules.memory] is not "
            "enabled in this agent's config"
        )
    did = _current_did.get()
    if not did:
        _fail_closed("no agent DID bound for the running turn", current_did=did)
    st = _registry.get(did)
    if st is None:
        _fail_closed("no memory state registered for the running agent", current_did=did)
    if st.agent_did != did:
        _fail_closed(
            "memory state DID does not match the running agent",
            current_did=did,
            resolved_did=st.agent_did,
        )
    return st


def state_for(agent_did: str) -> _State:
    """Return one agent's memory state by NAMING its DID, not by ambient binding.

    For work that runs on behalf of an agent outside a turn: the connected-data
    sync loop is a background task, not a descendant of the turn that bound the
    contextvar, so :func:`state` refused it with "no agent DID bound" — and
    approving a datastore mapping in the dashboard failed the moment the sync
    tried to attach the database to the Brain.

    This is no weaker than :func:`state`. The isolation rule is "never resolve a
    Brain that is not this agent's", and a caller passing a DID it already holds
    satisfies it more directly than an ambient one does: an unregistered or
    mismatched DID still fails closed and still audits. What it does not do is
    require the caller to be inside a turn, which background work is not.

    Args:
        agent_did: The agent this work is being done for.

    Raises:
        MemoryIsolationError: No state is registered for that DID.
        RuntimeError: The memory module was never configured in this process.
    """
    if not _registry:
        raise RuntimeError(
            "memory state read before the memory module was configured; the module is "
            "not installed at the deployment module root, or [modules.memory] is not "
            "enabled in this agent's config"
        )
    if not agent_did:
        _fail_closed("memory state requested without an agent DID", current_did="")
    st = _registry.get(agent_did)
    if st is None:
        _fail_closed("no memory state registered for that agent", current_did=agent_did)
    if st.agent_did != agent_did:
        _fail_closed(
            "memory state DID does not match the agent it was requested for",
            current_did=agent_did,
            resolved_did=st.agent_did,
        )
    return st


def bind(state_obj: _State) -> None:
    """Register ``state_obj`` under its DID and bind it as the current turn's agent.

    Called at the top of every turn-dispatch entry point (via
    ``activate_runtime_bindings``) so a turn running in a fresh sibling
    ``asyncio.Task`` — not a descendant of the task that ran ``configure()`` —
    resolves this agent's state. Cheap and idempotent: a dict insert plus one
    ``ContextVar.set``, no construction.
    """
    _registry[state_obj.agent_did] = state_obj
    _current_did.set(state_obj.agent_did)


def reset() -> None:
    """Test-only: clear all registered state and the current-DID binding."""
    _registry.clear()
    _current_did.set("")


def _fail_closed(reason: str, *, current_did: str, resolved_did: str = "") -> NoReturn:
    """Refuse a state read that cannot be tied to the running agent's DID.

    Logs the fault, best-effort emits a tamper-evident audit event, then raises
    :class:`MemoryIsolationError`. Never returns — the caller must not proceed
    with another agent's Brain.
    """
    _logger.error(
        "memory runtime isolation fault: %s (current=%r resolved=%r)",
        reason,
        current_did,
        resolved_did,
    )
    _emit_any_audit(
        "memory.isolation_fault",
        {
            "reason": reason,
            "current_did": current_did,
            "resolved_did": resolved_did,
            "registered_dids": sorted(_registry),
        },
    )
    raise MemoryIsolationError(reason)


def _emit_any_audit(action: str, detail: dict[str, Any]) -> None:
    """Emit a security audit event via any registered agent's telemetry sink.

    A fail-closed read (or a forged event for an unknown DID) may have no
    resolvable state, so there is no single obvious sink; the fleet's agents
    share the same tamper-evident audit backend, so recording the fault through
    any live sink is what matters. The caller's own ``_logger`` line is always
    emitted, so the event is never lost when no sink is available.
    """
    for st in _registry.values():
        telemetry = st.telemetry
        if telemetry is None:
            continue
        try:
            telemetry.audit_event(action, detail)
        except Exception:  # reason: audit failure must never mask the fail-closed refusal
            _logger.warning("failed to emit memory audit %s", action, exc_info=True)
        return


__all__ = [
    "MemoryIsolationError",
    "attach_shared_knowledge",
    "bind",
    "configure",
    "refuse_uncertified_shared_knowledge",
    "reset",
    "state",
    "state_for",
]
