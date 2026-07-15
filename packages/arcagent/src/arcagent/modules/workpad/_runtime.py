"""Per-agent runtime state for the workpad (self-managing ``context.md``) module.

The single ``agent:post_respond`` hook shares state — the eval config/model, the
run counter, the accumulated recent transcript, and the background-task set — via
:func:`state`.

Isolation model (SECURITY-CRITICAL — ASI03 / LLM02). A process runs many agents
concurrently (the embedded gateway caches up to 32). ``context.md`` is one
agent's private cockpit; rewriting it from another agent's transcript, or reading
its accumulated transcript into a different agent's turn, is a cross-agent bleed.
Mirrors :mod:`arcagent.modules.memory._runtime` exactly:

* **DID-keyed registry** — ``configure``/:func:`bind` register each agent's
  :class:`_State` under its own ``agent_did`` in :data:`_registry`; there is no
  single global slot to clobber (last-writer-wins is gone).
* **Fail-closed resolution** — :func:`state` resolves by the DID bound to the
  RUNNING turn (:data:`_current_did`), asserts the resolved state belongs to it,
  and refuses the read (raises :class:`WorkpadIsolationError` + audits) on any
  missing binding, missing registration, or DID mismatch — never falling back to
  ambient state.

The agent binds the current DID into every turn-dispatch task via :func:`bind`
(see ``activate_runtime_bindings``); that is now a correctness convenience, not
the sole leak guard, because the fail-closed resolution holds even if a rebind is
ever missed.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from arcagent.core.config import EvalConfig
from arcagent.modules.workpad.config import WorkpadConfig
from arcagent.utils.io import atomic_write_text

_logger = logging.getLogger("arcagent.modules.workpad._runtime")

# Cadence counters persist here so a process restart resumes mid-cadence instead
# of resetting run_count to 0 (the production box restarts every 1-5 minutes,
# which never let an in-memory counter reach every_n_runs).
_STATE_FILE = ".workpad-state.json"


class WorkpadIsolationError(RuntimeError):
    """Workpad state could not be resolved to the running agent's DID.

    Raised fail-closed rather than return another agent's cockpit/transcript
    (ASI03 / LLM02). Subclasses ``RuntimeError`` so callers that already handle
    the "workpad not configured" runtime error keep catching this too.
    """


@dataclass
class _State:
    """Mutable runtime state shared across the workpad hook + maintainer."""

    config: WorkpadConfig
    eval_config: EvalConfig
    workspace: Path
    telemetry: Any
    llm_config: Any
    eval_label: str
    agent_did: str
    eval_model: Any = None
    run_count: int = 0
    # Wall-clock time and run_count at the last maintenance trigger — persisted so
    # the idle-flush backstop accumulates across restarts.
    last_maintenance_ts: float = 0.0
    runs_at_last_maintenance: int = 0
    # Recent role-tagged activity lines accumulated since the last rewrite; drained
    # (snapshotted + cleared) when the maintainer fires. Bounded by config.
    transcript: list[str] = field(default_factory=list)
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None

    def persist(self) -> None:
        """Atomically write the cadence counters so a restart resumes mid-cadence."""
        atomic_write_text(
            self.workspace / _STATE_FILE,
            json.dumps(
                {
                    "run_count": self.run_count,
                    "last_maintenance_ts": self.last_maintenance_ts,
                    "runs_at_last_maintenance": self.runs_at_last_maintenance,
                }
            ),
        )


def _load_persisted(workspace: Path) -> dict[str, Any]:
    """Read the persisted cadence counters; tolerate a missing/corrupt file."""
    try:
        data = json.loads((workspace / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Per-agent state keyed by the owning agent's DID (never a single clobberable
# slot). Reads select one entry via the turn-bound DID below.
_registry: dict[str, _State] = {}

# The DID of the agent whose turn is running in THIS asyncio task. Empty outside
# a bound context, which makes state() fail closed rather than guess.
_current_did: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arcagent_workpad_current_did", default=""
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
    agent_did: str = "",
) -> None:
    """Build this agent's workpad state, register it, and bind its DID.

    Called once at agent startup. Registers under ``agent_did`` (never a single
    shared slot) and binds ``_current_did`` for the running startup task.
    """
    cfg = WorkpadConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = Path(workspace).resolve()
    persisted = _load_persisted(ws)
    new_state = _State(
        config=cfg,
        eval_config=ec,
        workspace=ws,
        telemetry=telemetry,
        llm_config=llm_config,
        eval_label=f"{agent_name}/workpad" if agent_name else "workpad",
        agent_did=agent_did,
        semaphore=asyncio.Semaphore(ec.max_concurrent),
        run_count=int(persisted.get("run_count", 0)),
        last_maintenance_ts=float(persisted.get("last_maintenance_ts", time.time())),
        runs_at_last_maintenance=int(persisted.get("runs_at_last_maintenance", 0)),
    )
    _registry[agent_did] = new_state
    _current_did.set(agent_did)
    if not persisted:
        # Seed the idle clock so it accumulates across restarts before the first flush.
        new_state.persist()
    _logger.info("workpad module configured (every_n_runs=%d)", cfg.every_n_runs)


def state() -> _State:
    """Return the running agent's workpad state, resolved by its bound DID.

    Fail-closed on identity: selected by the DID bound for the running turn,
    never by ambient last-writer-wins global. A missing binding, a missing
    registration, or a DID mismatch refuses the read (raises + audits) instead
    of handing back another agent's cockpit/transcript.
    """
    did = _current_did.get()
    if not did:
        _fail_closed("no agent DID bound for the running turn", current_did=did)
    st = _registry.get(did)
    if st is None:
        _fail_closed("no workpad state registered for the running agent", current_did=did)
    if st.agent_did != did:
        _fail_closed(
            "workpad state DID does not match the running agent",
            current_did=did,
            resolved_did=st.agent_did,
        )
    return st


def bind(state_obj: _State) -> None:
    """Register ``state_obj`` under its DID and bind it as the current turn's agent.

    Called at the top of every turn-dispatch entry point so a turn running in a
    fresh sibling ``asyncio.Task`` resolves this agent's state. Cheap and
    idempotent: a dict insert plus one ``ContextVar.set``.
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
    :class:`WorkpadIsolationError`. Never returns.
    """
    _logger.error(
        "workpad runtime isolation fault: %s (current=%r resolved=%r)",
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
    raise WorkpadIsolationError(reason)


def _emit_isolation_audit(detail: dict[str, Any]) -> None:
    """Emit the isolation-fault audit via any registered agent's telemetry sink.

    A fail-closed read may have no resolvable state; the fleet's agents share the
    same tamper-evident audit backend, so recording the fault through any live
    sink is what matters. The raise still happens even with no sink available.
    """
    for st in _registry.values():
        telemetry = st.telemetry
        if telemetry is None:
            continue
        try:
            telemetry.audit_event("workpad.isolation_fault", detail)
        except Exception:  # reason: audit failure must never mask the fail-closed raise
            _logger.warning("failed to emit workpad isolation audit", exc_info=True)
        return


__all__ = ["WorkpadIsolationError", "bind", "configure", "reset", "state"]
