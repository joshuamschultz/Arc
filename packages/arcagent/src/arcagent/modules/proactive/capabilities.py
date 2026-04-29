"""Decorator-form proactive module — SPEC-021 capability pattern.

A single ``@capability`` class :class:`ProactiveEngineCapability` owns the
:class:`ProactiveEngine` lifecycle: acquires leader election on setup,
starts the tick loop, then stops the tick loop, drains in-flight tasks,
and releases the leader lock on teardown.

One ``@hook("agent:shutdown")`` provides an additional shutdown path
alongside lifecycle teardown, matching the ``agent:shutdown`` subscription
declared in MODULE.yaml.

Runtime state lives in :mod:`arcagent.modules.proactive._runtime`. The
agent calls :func:`_runtime.configure` once at startup; the capability
class and hook read state lazily.

Why a noop handler at setup time? The engine requires a ``handler``
callable at construction. A noop handler is used as a defensive
placeholder; callers that need a real handler wire it via config or a
bus message after setup. The noop ensures the engine can be safely
constructed and started without blocking on a callback that may arrive
after module load.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arcagent.modules.proactive import _runtime
from arcagent.modules.proactive.engine import ProactiveEngine, Schedule
from arcagent.tools._decorator import capability, hook

_logger = logging.getLogger("arcagent.modules.proactive.capabilities")


@capability(name="proactive_engine")
class ProactiveEngineCapability:
    """Lifecycle-bound :class:`ProactiveEngine` wrapper.

    ``setup()`` acquires leader election, constructs the engine with the
    configured event sink wired to telemetry, and launches the tick loop
    as an asyncio task. If leader election fails the engine is NOT
    started — only the elected instance runs the tick loop, which is the
    correct multi-instance behaviour (R-048).

    ``teardown()`` stops the tick loop, drains all in-flight handler
    tasks, and releases the leader lock. Idempotent — safe to call
    multiple times.
    """

    async def setup(self, ctx: Any) -> None:
        del ctx  # Loader passes None; state lives in _runtime.
        st = _runtime.state()
        if st.engine is not None:
            return  # Idempotent: already set up.

        is_leader = await st.leader.acquire_or_wait()
        if not is_leader:
            _logger.info("ProactiveEngine: leader election not acquired; tick loop will not start")
            return

        engine = ProactiveEngine(
            handler=_noop_handler,
            event_sink=_make_event_sink(st.telemetry),
        )
        st.engine = engine

        # Start the tick loop as a detached asyncio task. The task handle
        # is stashed on state so teardown can cancel it cleanly.
        st._tick_task = asyncio.get_running_loop().create_task(
            engine.start_tick_loop(),
            name="proactive:tick_loop",
        )
        _logger.info("ProactiveEngine capability started (agent=%s)", st.agent_name)

    async def teardown(self) -> None:
        st = _runtime.state()
        engine = st.engine
        if engine is None:
            return

        # 1. Signal the tick loop to exit its while-loop.
        engine.stop()

        # 2. Cancel the asyncio task so the sleep(poll_interval) wakes
        #    immediately rather than waiting for the next polling cycle.
        _cancel_and_wait = _cancel_tick_task  # local alias for clarity
        await _cancel_and_wait(st._tick_task)
        st._tick_task = None

        # 3. Drain all in-flight handler tasks before releasing resources.
        await engine.drain()

        # 4. Release leader lock so another instance can take over.
        await st.leader.release()

        st.engine = None
        _logger.info("ProactiveEngine capability stopped (agent=%s)", st.agent_name)


@hook(event="agent:shutdown", trylast=True)
async def on_agent_shutdown(ctx: Any) -> None:
    """Ensure the engine is stopped on agent:shutdown.

    ``trylast=True`` so this runs after other shutdown hooks; the engine
    should be the last thing torn down to avoid missed ticks while other
    components are still active.

    If the capability lifecycle already ran teardown (the normal path),
    this is a no-op because ``state().engine`` will be ``None``.
    """
    del ctx
    st = _runtime.state()
    if st.engine is None:
        return
    engine = st.engine
    engine.stop()
    await _cancel_tick_task(st._tick_task)
    st._tick_task = None
    await engine.drain()
    await st.leader.release()
    st.engine = None
    _logger.info("ProactiveEngine stopped via agent:shutdown hook (agent=%s)", st.agent_name)


# --- Helpers ------------------------------------------------------------------


async def _cancel_tick_task(tick_task: Any) -> None:
    """Cancel a tick-loop asyncio task and await its completion.

    ``CancelledError`` is swallowed — it is the expected outcome of
    cancellation. Unexpected exceptions from the tick loop are logged
    rather than propagated so teardown always completes.
    """
    if tick_task is None or tick_task.done():
        return
    tick_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass  # Expected: the task was cancelled on request.
    except Exception:
        _logger.exception("ProactiveEngine tick loop raised during shutdown")


async def _noop_handler(schedule: Schedule) -> None:
    """Placeholder handler — logs a warning if it actually fires.

    In production callers register real schedules with their own handler
    callbacks before any tick is due. This handler exists only so the
    engine can be constructed before a real handler is wired in.
    """
    _logger.warning(
        "ProactiveEngine fired noop handler for schedule %r — no real handler wired",
        schedule.id,
    )


def _make_event_sink(
    telemetry: Any,
) -> Any:
    """Return an EventSink callable wired to telemetry, or None.

    If no telemetry is configured the engine runs without an event sink
    (structured events are silently dropped). This matches the graceful
    degradation contract in ProactiveEngine._emit.
    """
    if telemetry is None:
        return None

    def _sink(event: str, payload: dict[str, Any]) -> None:
        try:
            telemetry.emit(event, payload)
        except Exception:
            _logger.exception("ProactiveEngine event sink raised for event=%r", event)

    return _sink


__all__ = [
    "ProactiveEngineCapability",
    "on_agent_shutdown",
]
