"""Decorator-form pulse module — SPEC-021.

A single ``@capability`` class :class:`Pulse` owns the
:class:`PulseEngine` lifecycle (open on setup, stop on teardown). One
module-level ``@hook("agent:ready")`` binds the agent's ``run``
callback into the engine so the timer loop unblocks and begins firing
overdue checks.

Why ``@capability`` + ``@hook`` rather than ``@background_task``? Pulse
needs two distinct wiring points:

1. The engine must start at agent-setup time so the readiness gate
   (``asyncio.Event`` inside PulseEngine) is in place before any
   ``agent:ready`` event fires.
2. The real ``agent_run_fn`` arrives later via ``agent:ready`` data. The
   hook hands it to the already-running engine which then sets the
   readiness event and unblocks the timer loop.

A bare ``@background_task`` would own neither the engine lifecycle nor
the run-fn binding — those are explicit start/stop + event-driven steps,
exactly the contract ``@capability`` + ``@hook`` is designed for.

Runtime state lives in :mod:`arcagent.modules.pulse._runtime`. The
agent calls :func:`_runtime.configure` once at startup; the capability
class and hook read state lazily.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.pulse import _runtime
from arcagent.modules.pulse.engine import PulseEngine
from arcagent.tools._decorator import capability, hook

_logger = logging.getLogger("arcagent.modules.pulse.capabilities")


@capability(name="pulse")
class Pulse:
    """Lifecycle-bound :class:`PulseEngine` wrapper.

    ``setup()`` constructs the engine from configured workspace/config/
    bus and starts the internal timer loop. The timer loop waits on an
    ``asyncio.Event`` for a real ``agent_run_fn``; if one was supplied
    at configure time the engine is primed immediately, otherwise the
    ``agent:ready`` hook below provides it.

    ``teardown()`` calls :meth:`PulseEngine.stop` which cancels the
    timer task cleanly.
    """

    async def setup(self, ctx: Any) -> None:
        del ctx  # Loader passes None; state lives in _runtime.
        st = _runtime.state()
        if st.engine is not None:
            return  # Idempotent: already set up.

        run_fn = st.agent_run_fn or _noop_run_fn
        engine = PulseEngine(
            workspace=st.workspace,
            config=st.config,
            agent_run_fn=run_fn,
            bus=st.bus,
        )
        # Prime the readiness gate now if a real run_fn was supplied at
        # configure time so the timer loop doesn't wait unnecessarily.
        if st.agent_run_fn is not None:
            engine.set_agent_run_fn(st.agent_run_fn)

        await engine.start()
        st.engine = engine
        _logger.info("Pulse capability started (interval=%ds)", st.config.interval_seconds)

    async def teardown(self) -> None:
        st = _runtime.state()
        if st.engine is None:
            return
        await st.engine.stop()
        st.engine = None
        _logger.info("Pulse capability stopped")


@hook(event="agent:ready")
async def bind_agent_run_fn(ctx: Any) -> None:
    """Bind the agent's ``run`` callback into the engine on agent:ready.

    The agent emits ``agent:ready`` with ``data={"run_fn": <coro>}``.
    Setting it on the engine unblocks the timer loop's readiness gate so
    pulse checks can begin firing.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    run_fn = data.get("run_fn")
    if run_fn is None:
        return
    st = _runtime.state()
    st.agent_run_fn = run_fn
    if st.engine is not None:
        st.engine.set_agent_run_fn(run_fn)
        _logger.info("Bound agent_run_fn via agent:ready hook")


# --- Helpers ---------------------------------------------------------------


async def _noop_run_fn(prompt: str, **kwargs: Any) -> str:
    """Placeholder until ``agent:ready`` binds the real callback.

    The engine's timer loop is gated on a readiness ``asyncio.Event``
    until the real ``run_fn`` is bound, so this should never fire in
    practice. It exists as a defensive fallback so the engine can be
    constructed before the agent is fully wired.
    """
    del kwargs
    _logger.warning("Pulse fired before agent_run_fn bound; prompt=%r", prompt)
    return ""


__all__ = [
    "Pulse",
    "bind_agent_run_fn",
]
