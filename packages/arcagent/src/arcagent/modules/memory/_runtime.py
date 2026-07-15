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
from typing import Any, NoReturn

from arcagent.brain import Brain, NullBrain, select_brain
from arcagent.modules.memory.config import MemoryConfig

_logger = logging.getLogger("arcagent.modules.memory._runtime")

_RECALL_CACHE_CAP = 8


class MemoryIsolationError(RuntimeError):
    """Memory state could not be resolved to the running agent's DID.

    Raised fail-closed rather than return another agent's Brain — the severed
    cross-agent isolation check restored (ASI03 / LLM02). Subclasses
    ``RuntimeError`` so callers that already handle the "memory not configured"
    runtime error keep catching this too.
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
    # Once-per-turn recall cache: query-hash -> injectable text (bounds the
    # spawn double-assembly to a single retrieve).
    recall_cache: dict[int, str] = field(default_factory=dict)
    # Consolidation trigger bookkeeping.
    events_since_consolidate: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    last_consolidate_at: float = field(default_factory=time.monotonic)


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
) -> None:
    """Build this agent's Brain-backed state, register it, and bind its DID.

    Called once at agent startup. Registers under ``agent_did`` (never a single
    shared slot) and binds ``_current_did`` for the running startup task so the
    background tasks spawned there copy this agent's DID and stay pinned to it.
    ``identity`` (the agent's signer) and ``policy_pipeline`` are threaded to the
    Brain so the agentic consolidation engine's memory-tool writes are signed +
    policy-authorized.
    """
    del agent_name  # accepted for signature-dispatch parity; unused here
    cfg = MemoryConfig(**(config or {}))
    ws = Path(workspace).resolve()
    brain = select_brain(
        cfg.brain,
        workspace=ws,
        agent_did=agent_did,
        tier=cfg.tier,
        brain_allowlist=tuple(cfg.brain_allowlist),
        identity=identity,
        policy_pipeline=policy_pipeline,
        backend_config=dict(cfg.backend),
    )
    new_state = _State(
        config=cfg,
        brain=brain,
        workspace=ws,
        telemetry=telemetry,
        bus=bus,
        agent_did=agent_did,
        active=not isinstance(brain, NullBrain),
    )
    _registry[agent_did] = new_state
    _current_did.set(agent_did)
    _logger.info("memory module configured (brain=%s, active=%s)", cfg.brain, new_state.active)


def state() -> _State:
    """Return the running agent's memory state, resolved by its bound DID.

    Fail-closed on identity: the state is selected by the DID bound for the
    running turn, never by ambient last-writer-wins global. A missing binding,
    a missing registration, or a DID mismatch refuses the read (raises + audits)
    instead of handing back another agent's Brain.
    """
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
    _emit_isolation_audit(
        {
            "reason": reason,
            "current_did": current_did,
            "resolved_did": resolved_did,
            "registered_dids": sorted(_registry),
        }
    )
    raise MemoryIsolationError(reason)


def _emit_isolation_audit(detail: dict[str, Any]) -> None:
    """Emit the isolation-fault audit via any registered agent's telemetry sink.

    A fail-closed read may have no resolvable state, so there is no single
    obvious sink; the fleet's agents share the same tamper-evident audit
    backend, so recording the fault through any live sink is what matters. The
    raise still happens even if no sink is available (the ``_logger.error`` in
    :func:`_fail_closed` is always emitted).
    """
    for st in _registry.values():
        telemetry = st.telemetry
        if telemetry is None:
            continue
        try:
            telemetry.audit_event("memory.isolation_fault", detail)
        except Exception:  # reason: audit failure must never mask the fail-closed raise
            _logger.warning("failed to emit memory isolation audit", exc_info=True)
        return


__all__ = ["MemoryIsolationError", "bind", "configure", "reset", "state"]
