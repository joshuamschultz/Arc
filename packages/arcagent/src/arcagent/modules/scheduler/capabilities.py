"""Decorator-form scheduler module — SPEC-021 task 3.2.

A single ``@capability`` class :class:`Scheduler` owns the
:class:`SchedulerEngine` lifecycle (open on setup, stop + drain on
teardown). Four module-level ``@tool`` functions expose the CRUD
surface the LLM uses to manage schedules. One module-level
``@hook("agent:ready")`` binds the agent's ``run`` callback into the
engine so the timer loop unblocks and starts firing.

Runtime state lives in :mod:`arcagent.modules.scheduler._runtime`. The
agent calls :func:`_runtime.configure` once at startup; the capability
class, hook, and tools all read state lazily.

Why module-level tools instead of methods on :class:`Scheduler`? The
loader's :class:`CapabilityClassMetadata` path instantiates the class
with no arguments and registers any ``@tool``-stamped methods bound to
that instance. The scheduler's tools need shared store/config/telemetry
which already live on ``_runtime.state()`` — going through the class
instance would just add an indirection that buys nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arcagent.modules.scheduler import _runtime
from arcagent.modules.scheduler.models import (
    ScheduleEntry,
    generate_schedule_id,
    validate_prompt,
)
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.tools._decorator import capability, hook, tool

_logger = logging.getLogger("arcagent.modules.scheduler.capabilities")

# Fields that schedule_update is allowed to modify. Mirrors tools.py.
_UPDATABLE_FIELDS = frozenset(
    {
        "prompt",
        "enabled",
        "expression",
        "every_seconds",
        "timeout_seconds",
        "active_hours",
    }
)


@capability(name="scheduler")
class Scheduler:
    """Lifecycle-bound :class:`SchedulerEngine` wrapper.

    ``setup()`` constructs the engine against the configured store /
    config / telemetry / bus and starts the timer + worker tasks. If a
    real ``agent_run_fn`` was bound at configure time, the engine is
    primed immediately; otherwise the timer loop waits on the
    ``agent:ready`` hook below to bind one.

    ``teardown()`` calls :meth:`SchedulerEngine.stop` which already
    cancels the timer task, drains the in-flight queue (timeout
    bounded), and cancels the worker.
    """

    async def setup(self, ctx: Any) -> None:
        del ctx  # Loader passes None; state lives in _runtime.
        st = _runtime.state()
        if st.engine is not None:
            return  # Idempotent: already set up.

        run_fn = st.agent_run_fn or _noop_run_fn
        engine = SchedulerEngine(
            store=st.store,
            config=st.config,
            telemetry=st.telemetry,
            agent_run_fn=run_fn,
            bus=st.bus,
        )
        # If a real run_fn was provided at configure time, mark the
        # engine ready so the timer loop doesn't block waiting for one.
        if st.agent_run_fn is not None:
            engine.set_agent_run_fn(st.agent_run_fn)

        await engine.start()
        st.engine = engine
        _logger.info("Scheduler capability started")

    async def teardown(self) -> None:
        st = _runtime.state()
        if st.engine is None:
            return
        await st.engine.stop()
        st.engine = None
        _logger.info("Scheduler capability stopped")


@hook(event="agent:ready")
async def bind_agent_run_fn(ctx: Any) -> None:
    """Bind the agent's ``run`` callback into the engine on agent:ready.

    The agent emits ``agent:ready`` with ``data={"run_fn": <coro>}``.
    Setting it on the engine unblocks the timer loop's readiness gate
    so schedules can begin firing.
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


# --- CRUD tools -----------------------------------------------------------


@tool(
    name="schedule_create",
    description="Create a new scheduled task (cron, interval, or one-time)",
    classification="state_modifying",
)
async def schedule_create(
    type: str = "interval",  # noqa: A002 - matches JSON schema field name
    prompt: str = "",
    expression: str | None = None,
    at: str | None = None,
    every_seconds: int | None = None,
    active_hours: dict[str, Any] | None = None,
    timeout_seconds: int = 300,
) -> str:
    """Create a new schedule. Enforces quota and prompt validation."""
    st = _runtime.state()
    try:
        existing = st.store.load()
        if len(existing) >= st.config.max_schedules:
            return json.dumps(
                {"error": f"Schedule quota exceeded (max {st.config.max_schedules})"}
            )

        validate_prompt(prompt, max_length=st.config.max_prompt_length)

        entry = ScheduleEntry.model_validate(
            {
                "id": generate_schedule_id(),
                "type": type,
                "prompt": prompt,
                "expression": expression,
                "at": at,
                "every_seconds": every_seconds,
                "active_hours": active_hours,
                "timeout_seconds": timeout_seconds,
            }
        )
        st.store.add(entry)
        _logger.info("Created schedule %s (type=%s)", entry.id, type)
        return entry.model_dump_json()
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="schedule_list",
    description="List all scheduled tasks",
    classification="read_only",
)
async def schedule_list(enabled_only: bool = False) -> str:
    """List all schedules, optionally filtered to enabled-only."""
    st = _runtime.state()
    entries = st.store.load()
    if enabled_only:
        entries = [e for e in entries if e.enabled]
    return json.dumps([e.model_dump() for e in entries])


@tool(
    name="schedule_update",
    description="Update an existing scheduled task",
    classification="state_modifying",
)
async def schedule_update(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    prompt: str | None = None,
    enabled: bool | None = None,
    expression: str | None = None,
    every_seconds: int | None = None,
    timeout_seconds: int | None = None,
    active_hours: dict[str, Any] | None = None,
) -> str:
    """Update an existing schedule with allowlisted fields only."""
    st = _runtime.state()
    candidates: dict[str, Any] = {
        "prompt": prompt,
        "enabled": enabled,
        "expression": expression,
        "every_seconds": every_seconds,
        "timeout_seconds": timeout_seconds,
        "active_hours": active_hours,
    }
    updates = {k: v for k, v in candidates.items() if v is not None and k in _UPDATABLE_FIELDS}
    if not updates:
        return json.dumps({"error": "No updatable fields provided"})
    try:
        if "prompt" in updates:
            validate_prompt(updates["prompt"], max_length=st.config.max_prompt_length)
        updated = st.store.update(id, updates)
        _logger.info("Updated schedule %s", id)
        return updated.model_dump_json()
    except KeyError:
        return json.dumps({"error": f"Schedule '{id}' not found"})
    except (ValueError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="schedule_cancel",
    description="Cancel (disable) or delete a scheduled task",
    classification="state_modifying",
)
async def schedule_cancel(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    delete: bool = False,
) -> str:
    """Disable a schedule, or delete it if ``delete=True``."""
    st = _runtime.state()
    try:
        if delete:
            st.store.remove(id)
            _logger.info("Deleted schedule %s", id)
            return json.dumps({"status": "deleted", "id": id})
        st.store.update(id, {"enabled": False})
        _logger.info("Disabled schedule %s", id)
        return json.dumps({"status": "disabled", "id": id})
    except KeyError:
        return json.dumps({"error": f"Schedule '{id}' not found"})


# --- Helpers --------------------------------------------------------------


async def _noop_run_fn(prompt: str, **kwargs: Any) -> str:
    """Placeholder until ``agent:ready`` binds the real callback.

    The engine's timer loop is gated on a readiness :class:`asyncio.Event`
    until the real ``run_fn`` is bound, so this should never fire in
    practice. It exists as a defensive fallback so the engine can be
    constructed before the agent is fully wired.
    """
    del kwargs
    _logger.warning("Scheduler fired before agent_run_fn bound; prompt=%r", prompt)
    return ""


__all__ = [
    "Scheduler",
    "bind_agent_run_fn",
    "schedule_cancel",
    "schedule_create",
    "schedule_list",
    "schedule_update",
]
