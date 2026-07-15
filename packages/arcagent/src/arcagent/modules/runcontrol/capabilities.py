"""Run-control capabilities — the per-agent operator kill-switch watcher.

A ``@hook`` captures the live agent at ``agent:ready``; a ``@background_task``
polls the shared ``cancellations`` directory and, for each pending request,
resolves the matching live :class:`arcrun.RunHandle` in the agent's tracked-run
map and calls ``cancel(caller_did, reason)`` — a cooperative, attributable stop
(ASI09/ASI10). Only tracked runs (``agent._active_runs``) are reachable this way;
streaming/chat runs expose no handle (GAP-A, deferred).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arcstore.cancellations import CancelRequest

from arcagent.modules.runcontrol import _runtime
from arcagent.tools._decorator import background_task, hook

_logger = logging.getLogger("arcagent.modules.runcontrol.capabilities")

# Watcher poll cadence (seconds). Operator "stop" latency is bounded by this; a
# handful of pending-row reads per tick is cheap even at fleet scale. Mirrors the
# tasks loops' module-constant cadence (the @background_task interval is fixed at
# decoration time, so the loop body sleeps on the same constant).
_WATCH_TICK = 3.0


def _find_handle(agent: Any, req: CancelRequest) -> tuple[str, Any] | None:
    """Resolve a cancel request to a live ``(session_key, RunHandle)``, or None.

    Matches on ``run_id`` first (the cross-surface identifier the arcui timeline
    and traces join on), then falls back to ``session_key`` (the agent's
    ``_active_runs`` key). arcagent exposes no public enumerator over live runs, so
    the watcher reads the tracked-run map directly — read-only, in-process, and the
    only seam available without editing the agent orchestrator.
    """
    active: dict[str, Any] = getattr(agent, "_active_runs", {})
    for session_key, handle in list(active.items()):
        if req.run_id and getattr(handle.state, "run_id", "") == req.run_id:
            return session_key, handle
        if req.session_key and session_key == req.session_key:
            return session_key, handle
    return None


async def _apply_cancel(st: _runtime._State, req: CancelRequest) -> None:
    """Cancel the live run a request names and mark it applied (attributed).

    No live handle → leave the request pending: the run may not have started yet,
    or it is a streaming run this cooperative path can't reach (GAP-A);
    :func:`_sweep_stale` ages it out once it exceeds the TTL. On a hit, the run is
    stopped via its handle (carrying the operator DID as ``caller_did``) and the
    request is resolved ``applied`` race-safely; an explicit audit event names the
    operator.
    """
    match = _find_handle(st.agent, req)
    if match is None:
        return
    session_key, handle = match
    await handle.cancel(req.requested_by, req.reason or None)
    resolved = await st.store.resolve(
        req.id,
        status="applied",
        actor_did=req.requested_by,
        resolved_by=req.requested_by,
        note=f"cancelled run for session {session_key}",
    )
    if resolved is not None and st.telemetry is not None:
        # Operator-attributed audit at the point of application (ASI09/ASI10) —
        # complements the ``loop.cancelled`` event arcrun emits in the run's own
        # signed event stream.
        st.telemetry.audit_event(
            "run.cancel.applied",
            {
                "caller_did": req.requested_by,
                "run_id": req.run_id,
                "session_key": session_key,
                "reason": req.reason,
            },
        )


async def _sweep_stale(st: _runtime._State) -> None:
    """Age out pending requests that never matched a live run (operator visibility).

    Runs every tick regardless of agent readiness — a request whose target already
    ended, or a streaming run this cooperative path can't reach (GAP-A), would
    otherwise sit ``pending`` forever. The store sweep is race-safe (it shares the
    conditional ``resolve`` claim), so an apply in the same tick still wins; each
    age-out gets an operator-attributed audit event.
    """
    try:
        expired = await st.store.expire_stale(
            ttl_seconds=st.config.stale_ttl_seconds, actor_did=st.identity.did
        )
    except Exception:  # reason: fail-open — a sweep error must never stall the watcher
        _logger.warning("runcontrol: stale-cancel sweep failed", exc_info=True)
        return
    for req in expired:
        if st.telemetry is not None:
            st.telemetry.audit_event(
                "run.cancel.expired",
                {
                    "caller_did": req.requested_by,
                    "run_id": req.run_id,
                    "session_key": req.session_key,
                    "reason": req.reason,
                },
            )


async def _watch_tick() -> None:
    """One watcher pass: apply matching cancels, then age out stale unmatched ones."""
    await _runtime.ensure_store()
    st = _runtime.state()
    if st.agent is not None:
        for req in await st.store.list(status="pending"):
            try:
                await _apply_cancel(st, req)
            except Exception:  # reason: fail-open — one bad request must not stall the watcher
                _logger.warning("runcontrol: failed to apply cancel %s", req.id, exc_info=True)
    await _sweep_stale(st)


@hook(event="agent:ready", priority=100)
async def runcontrol_bind_agent(ctx: Any) -> None:
    """Capture the live agent for the watcher (mirrors tasks' run_fn binding).

    The ``agent:ready`` payload carries ``run_fn`` (``ArcAgent.run_collected``, a
    bound method); ``run_fn.__self__`` is the agent whose tracked-run map the
    watcher resolves cancel requests against.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    run_fn = data.get("run_fn")
    agent = getattr(run_fn, "__self__", None)
    if agent is not None:
        _runtime.state().agent = agent


@background_task(name="runcontrol_watcher", interval=_WATCH_TICK)
async def runcontrol_watcher(_ctx: Any) -> None:
    """Background loop: apply operator cancel requests to live tracked runs.

    The loader spawns this once (``register_task`` calls ``fn(None)`` a single
    time), so the ``while True`` MUST live here or the watcher runs one tick and
    dies (mirrors the tasks loops).
    """
    while True:
        try:
            await _watch_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a tick error must never crash the agent
            _logger.warning("runcontrol watch tick failed", exc_info=True)
        await asyncio.sleep(_WATCH_TICK)


__all__ = ["runcontrol_bind_agent", "runcontrol_watcher"]
