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
from datetime import UTC, datetime
from typing import Any

from arcagent.core import turn_context
from arcagent.core.control_contract import (
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
)
from arcagent.modules.scheduler import _runtime
from arcagent.modules.scheduler.models import (
    ScheduleEntry,
    generate_schedule_id,
    validate_prompt,
)
from arcagent.modules.scheduler.occurrence import next_fire_at
from arcagent.modules.scheduler.operator_notice import (
    format_schedule_notice,
    notify_operator,
)
from arcagent.modules.scheduler.registration import register_schedule_revision
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.tools._decorator import capability, hook, tool

_logger = logging.getLogger("arcagent.modules.scheduler.capabilities")


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

        # No placeholder callback. A noop that "succeeds" would mark a
        # reminder run and disable it — the reminder would be gone and nobody
        # told. None means "not bound yet", which the engine reports and leaves
        # the row pending for.
        engine = SchedulerEngine(
            store=st.store,
            config=st.config,
            telemetry=st.telemetry,
            agent_run_fn=st.agent_run_fn,
            bus=st.bus,
            control_artifact_authority=st.control_artifact_authority,
            control_tenant_id=st.control_tenant_id,
            agent_did=st.agent_did,
            trigger_issuer=st.trigger_issuer,
            prepare_collected_request=st.prepare_collected_request,
            accepted_reply_fn=st.accepted_reply_fn,
            reply_send=st.reply_send,
            reply_lookup=st.reply_lookup,
        )
        # If a real run_fn was provided at configure time, mark the
        # engine ready so the timer loop doesn't block waiting for one.
        engine.label = str(st.workspace)
        # Bound before this engine existed, bound after it started, or bound in
        # a different asyncio task: all the same to the engine, which asks for a
        # callback when it has work and none in hand.
        workspace = st.workspace
        engine.run_fn_resolver = lambda: _runtime.recall_run_fn(workspace)

        await engine.start()
        st.engine = engine
        _logger.info("Scheduler capability started")

        # Backfill schedules for any workflow whose trigger has no entry yet, so
        # a workflow set "on a schedule" actually fires and shows up in the store.
        # Best-effort inside the sync itself: a deployment without the workflows
        # module simply has nothing to reconcile.
        from arcagent.modules.scheduler.workflow_sync import reconcile_workflow_schedules

        await reconcile_workflow_schedules()

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

    The agent emits ``agent:ready`` with ``data={"run_fn": <coro>}``, where
    ``run_fn`` is ``ArcAgent.run_collected(input, *, session_key)``. Setting it
    on the engine unblocks the timer loop's readiness gate so schedules can
    begin firing.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    run_fn = data.get("run_fn")
    if run_fn is None:
        return

    st = _runtime.state()
    st.agent_run_fn = run_fn
    st.accepted_reply_fn = data.get("accepted_reply_fn")
    st.reply_send = data.get("scheduled_reply_send")
    st.reply_lookup = data.get("scheduled_reply_lookup")
    st.channel_deliver_fn = data.get("channel_deliver_fn")
    # Remember it against the agent's workspace as well: the engine may live in
    # a different asyncio task, where this state object is not the one it reads.
    _runtime.remember_run_fn(st.workspace, run_fn)
    if st.engine is None:
        # Ready arrived before setup. Nothing to do and nothing lost: setup
        # reads the callback off this state when it builds the engine.
        return
    st.engine.set_agent_run_fn(run_fn)
    st.engine.set_reply_delivery(st.accepted_reply_fn, st.reply_send, st.reply_lookup)
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
    timeout_seconds: int | None = None,
    deliver_to: str | None = None,
) -> str:
    """Create a new schedule. Enforces quota and prompt validation.

    ``deliver_to`` ("platform:chat_id") routes the run's output back to a
    channel; leave unset to keep the result internal. Defaults to the current
    conversation's channel when created during a chat turn (see _runtime).
    """
    st = _runtime.state()
    try:
        existing = st.store.load()
        if len(existing) >= st.config.max_schedules:
            return json.dumps(
                {"error": f"Schedule quota exceeded (max {st.config.max_schedules})"}
            )

        validate_prompt(prompt, max_length=st.config.max_prompt_length)

        resolved_timeout = (
            timeout_seconds if timeout_seconds is not None else st.config.default_timeout_seconds
        )
        # Default delivery to the channel this turn arrived on — but only a gateway
        # platform target. Schedule delivery goes through the gateway's
        # channel_deliver_fn, which cannot reach an arcteam channel (``channel://``),
        # so a team origin defaults to no auto-delivery rather than a target that
        # would silently drop when the schedule fires.
        default_channel = turn_context.inbound_channel()
        if default_channel and turn_context.is_team_target(default_channel):
            default_channel = None
        entry = ScheduleEntry.model_validate(
            {
                "id": generate_schedule_id(),
                "type": type,
                "prompt": prompt,
                "expression": expression,
                "at": at,
                "every_seconds": every_seconds,
                "active_hours": active_hours,
                "timeout_seconds": resolved_timeout,
                "deliver_to": deliver_to or default_channel,
            },
            context=st.config.validation_context(),
        )
        if (
            st.control_artifact_authority is None
            or st.control_tenant_id is None
            or st.control_actor_proof_source is None
        ):
            raise ControlArtifactUnavailableError("signed schedule registration unavailable")
        entry = await register_schedule_revision(
            entry,
            previous=None,
            tenant_id=st.control_tenant_id,
            agent_did=st.agent_did,
            authority=st.control_artifact_authority,
            actor_proof_source=st.control_actor_proof_source,
        )
        st.store.add(entry)
        _logger.info("Created schedule %s (type=%s)", entry.id, type)
        return entry.model_dump_json()
    except (
        ValueError,
        TypeError,
        ControlArtifactRefusedError,
        ControlArtifactUnavailableError,
    ) as exc:
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
    now = datetime.now(UTC)
    rows = []
    for entry in entries:
        row = entry.model_dump(mode="json")
        following = next_fire_at(entry, now, default_timezone=st.config.timezone or "UTC")
        row["next_fire_at"] = None if following is None else following.isoformat()
        rows.append(row)
    return json.dumps(rows)


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
    deliver_to: str | None = None,
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
        "deliver_to": deliver_to,
    }
    updates = {k: v for k, v in candidates.items() if v is not None}
    if not updates:
        return json.dumps({"error": "No updatable fields provided"})
    try:
        if "prompt" in updates:
            validate_prompt(updates["prompt"], max_length=st.config.max_prompt_length)
        prior = st.store.get(id)
        if prior is None:
            raise KeyError(id)
        candidate = ScheduleEntry.model_validate(
            {**prior.model_dump(), **updates}, context=st.config.validation_context()
        )
        candidate = _with_disable_reason(prior, candidate)
        if (
            st.control_artifact_authority is None
            or st.control_tenant_id is None
            or st.control_actor_proof_source is None
        ):
            raise ControlArtifactUnavailableError("signed schedule registration unavailable")
        updated = await register_schedule_revision(
            candidate,
            previous=prior,
            tenant_id=st.control_tenant_id,
            agent_did=st.agent_did,
            authority=st.control_artifact_authority,
            actor_proof_source=st.control_actor_proof_source,
        )
        st.store.update(id, updated.model_dump(), context=st.config.validation_context())
        _logger.info("Updated schedule %s", id)
        return updated.model_dump_json()
    except KeyError:
        return json.dumps({"error": f"Schedule '{id}' not found"})
    except (
        ValueError,
        TypeError,
        ControlArtifactRefusedError,
        ControlArtifactUnavailableError,
    ) as exc:
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
        prior = st.store.get(id)
        if prior is None:
            raise KeyError(id)
        if (
            st.control_artifact_authority is None
            or st.control_tenant_id is None
            or st.control_actor_proof_source is None
        ):
            raise ControlArtifactUnavailableError("signed schedule registration unavailable")
        disabled = await register_schedule_revision(
            _with_disable_reason(prior, prior.model_copy(update={"enabled": False})),
            previous=prior,
            tenant_id=st.control_tenant_id,
            agent_did=st.agent_did,
            authority=st.control_artifact_authority,
            actor_proof_source=st.control_actor_proof_source,
        )
        st.store.update(id, disabled.model_dump())
        if delete:
            st.store.remove(id)
            _logger.info("Deleted schedule %s", id)
            return json.dumps({"status": "deleted", "id": id})
        _logger.info("Disabled schedule %s", id)
        return json.dumps({"status": "disabled", "id": id})
    except KeyError:
        return json.dumps({"error": f"Schedule '{id}' not found"})
    except (
        ValueError,
        TypeError,
        ControlArtifactRefusedError,
        ControlArtifactUnavailableError,
    ) as exc:
        return json.dumps({"error": str(exc)})


# --- Helpers --------------------------------------------------------------


def _with_disable_reason(prior: ScheduleEntry, candidate: ScheduleEntry) -> ScheduleEntry:
    """Stamp WHY a row is off when a person turns it off, and clear it when turned on.

    Only a deliberate off is ``operator``; the breaker's own trip is stamped by the
    engine, and that is the one that re-arms itself.
    """
    if prior.enabled and not candidate.enabled:
        meta = candidate.metadata.model_copy(
            update={
                "disabled_reason": "operator",
                "disabled_at": datetime.now(UTC).isoformat(),
                "next_fire_at": None,
            }
        )
        return candidate.model_copy(update={"metadata": meta})
    if candidate.enabled and not prior.enabled:
        meta = candidate.metadata.model_copy(
            update={"disabled_reason": None, "disabled_at": None, "consecutive_failures": 0}
        )
        return candidate.model_copy(update={"metadata": meta})
    return candidate


@hook(event="schedule:failed")
async def notify_operator_of_failure(ctx: Any) -> None:
    """Tell the operator a schedule failed, and when the breaker has switched it off."""
    await _deliver_notice("schedule:failed", ctx)


@hook(event="schedule:missed")
async def notify_operator_of_missed_fire(ctx: Any) -> None:
    """Tell the operator a schedule is past due and has not started."""
    await _deliver_notice("schedule:missed", ctx)


@hook(event="schedule:rearmed")
async def notify_operator_of_rearm(ctx: Any) -> None:
    """Tell the operator a tripped schedule has switched itself back on."""
    await _deliver_notice("schedule:rearmed", ctx)


async def _deliver_notice(event: str, ctx: Any) -> None:
    data = ctx.data if hasattr(ctx, "data") else {}
    await notify_operator(
        _runtime.state(),
        format_schedule_notice(event, data),
        fallback_target=data.get("deliver_to"),
    )


__all__ = [
    "Scheduler",
    "bind_agent_run_fn",
    "schedule_cancel",
    "schedule_create",
    "schedule_list",
    "schedule_update",
]
