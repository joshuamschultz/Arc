"""Materialise a workflow's declared trigger into a real schedule entry.

A workflow declares *when* it runs in its own document (``[trigger]``), but the
only thing that actually fires cron is the scheduler store — one loop, over
``schedules.json``. Nothing bridged the two, so a workflow set "on a schedule"
in the dashboard sat inert: a correct predicate behind dead activating wiring,
this repository's recurring failure mode.

This module is that bridge, and it runs in ONE direction: a triggered workflow
becomes a ``workflow_run`` schedule entry the scheduler already knows how to
dispatch (SPEC-061 COMP-017). The entry lands in ``schedules.json`` like any
other, so it fires AND an operator can see and tune it there.

Two entry points, two intents:

* :func:`reconcile_workflow_schedules` — startup backfill. Creates a schedule for
  any triggered workflow that lacks one, and prunes derived schedules whose
  workflow lost its trigger or was archived/removed. It **creates-if-absent** and
  never overwrites an existing entry, so an operator's edit in ``schedules.json``
  survives every restart — the store is authoritative once the entry exists.
* :func:`sync_workflow_schedule` — the push. Called the moment a trigger is set
  in the builder, so the schedule appears immediately rather than at next boot.
  It applies the new *timing* to an existing entry while preserving the
  operator's ``enabled`` / delivery / timeout, because changing a trigger is an
  intentional act but disabling its schedule was one too.

The workflow bundles are read straight off the shared deployment root, not
through the workflows module's per-agent runtime: the backfill runs at
capability-setup time, before that module's ``ContextVar`` is reliably bound in
this task, and the store is deployment-wide anyway. Every failure is swallowed
with a log — a deployment without arcteam's workflow engine simply has no
workflow schedules to reconcile, never a broken scheduler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from arcagent.modules.scheduler import _runtime
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import (
    ActiveHours,
    ScheduleEntry,
    ScheduleMetadata,
)
from arcagent.modules.scheduler.store import ScheduleStore

_logger = logging.getLogger("arcagent.modules.scheduler.workflow_sync")

#: Every derived entry's id is this prefix plus the workflow id, so the bridge
#: can tell its own rows from an operator's hand-made ``workflow_run`` schedule
#: and prune only the former.
DERIVED_ID_PREFIX = "wf:"

# Fields the push path rewrites on an existing entry — the timing only. enabled,
# deliver_to, timeout_seconds, and metadata are the operator's and stay put.
_TIMING_FIELDS = ("type", "expression", "at", "every_seconds", "timezone", "active_hours")


def derived_id(workflow_id: str) -> str:
    """The deterministic schedule id for ``workflow_id``'s trigger."""
    return f"{DERIVED_ID_PREFIX}{workflow_id}"


def _now() -> str:
    """Current instant as an ISO string, for anchoring a new schedule."""
    return datetime.now(UTC).isoformat()


def parse_cron_tz(expression: str) -> tuple[str | None, str]:
    """Split a ``CRON_TZ=<zone> <expr>`` prefix off a cron expression.

    croniter rejects the inline ``CRON_TZ=`` form, so the zone is lifted into the
    entry's ``timezone`` field and the bare five-field expression stored. A plain
    expression comes back unchanged with no zone.
    """
    expr = expression.strip()
    if expr[:8].upper() == "CRON_TZ=":
        parts = expr.split(None, 1)
        zone = parts[0].split("=", 1)[1]
        rest = parts[1].strip() if len(parts) > 1 else ""
        return (zone or None), rest
    return None, expr


def _active_hours(trigger_hours: Any) -> ActiveHours | None:
    """Map a workflow trigger's active-hours window to the scheduler's shape."""
    if trigger_hours is None:
        return None
    return ActiveHours(
        start=trigger_hours.start,
        end=trigger_hours.end,
        timezone=trigger_hours.timezone,
    )


def desired_entry(
    workflow_id: str,
    trigger: Any,
    config: SchedulerConfig,
    *,
    anchor: str | None = None,
) -> ScheduleEntry | None:
    """The schedule a triggered workflow should have, or ``None`` for no schedule.

    ``None`` covers a missing trigger, a manual-only trigger, and any trigger
    whose timing the scheduler cannot honour (a bare-invalid cron, an interval
    below the module floor) — reported once, never materialised as a row that
    can never fire.

    ``anchor`` (an ISO timestamp) seeds ``last_run`` so a freshly wired nightly
    trigger waits for its next real slot instead of catch-up-firing the instant
    it is created — wiring "run at 10pm" at noon must not run it at noon.
    """
    trigger_type = getattr(trigger, "type", None)
    if trigger is None or trigger_type == "manual":
        return None

    document: dict[str, Any] = {
        "id": derived_id(workflow_id),
        "action": "workflow_run",
        "workflow_id": workflow_id,
        "prompt": "",  # a typed action carries no prose (LLM01)
        "timeout_seconds": config.default_timeout_seconds,
        "active_hours": _active_hours(getattr(trigger, "active_hours", None)),
        "metadata": ScheduleMetadata(
            created_by="system",
            reason=f"derived from workflow '{workflow_id}' trigger",
            last_run=anchor,
        ),
    }

    if trigger_type == "cron":
        expression = getattr(trigger, "expression", None)
        if not expression:
            return None
        zone, bare = parse_cron_tz(expression)
        document.update(type="cron", expression=bare, timezone=zone)
    elif trigger_type == "interval":
        interval = getattr(trigger, "interval_s", None)
        if not interval:
            return None
        document.update(type="interval", every_seconds=interval)
    else:
        return None

    try:
        return ScheduleEntry.model_validate(document, context=config.validation_context())
    except ValueError as exc:
        _logger.warning(
            "workflow '%s' trigger does not yield a valid schedule and will not "
            "fire until corrected: %s",
            workflow_id,
            exc,
        )
        return None


def _is_derived(entry: ScheduleEntry) -> bool:
    """Whether the bridge wrote this entry — the only rows it may prune/overwrite."""
    return (
        entry.action == "workflow_run"
        and entry.id.startswith(DERIVED_ID_PREFIX)
        and entry.metadata.created_by == "system"
    )


def full_reconcile(
    store: ScheduleStore,
    config: SchedulerConfig,
    triggers: Mapping[str, Any],
    *,
    anchor: str | None = None,
) -> None:
    """Create-if-absent for every triggered workflow; prune orphaned derived rows.

    ``triggers`` maps EVERY currently-known workflow id to its effective trigger
    (``None`` when archived or trigger-less). An existing entry is left untouched
    so an operator edit in the store wins; a derived entry with no corresponding
    desired schedule — trigger removed, workflow deleted — is dropped. ``anchor``
    seeds ``last_run`` on newly created entries so they wait for their next slot.
    """
    existing = {entry.id: entry for entry in store.load()}
    wanted: set[str] = set()

    for workflow_id, trigger in triggers.items():
        entry = desired_entry(workflow_id, trigger, config, anchor=anchor)
        if entry is None:
            continue
        wanted.add(entry.id)
        if entry.id not in existing:
            store.add(entry)

    for entry_id, entry in existing.items():
        if _is_derived(entry) and entry_id not in wanted:
            store.remove(entry_id)


def push_one(
    store: ScheduleStore,
    config: SchedulerConfig,
    workflow_id: str,
    trigger: Any,
    *,
    anchor: str | None = None,
) -> None:
    """Materialise (or update, or remove) one workflow's schedule right now."""
    entry = desired_entry(workflow_id, trigger, config, anchor=anchor)
    schedule_id = derived_id(workflow_id)
    current = store.get(schedule_id)

    if entry is None:
        if current is not None and _is_derived(current):
            store.remove(schedule_id)
        return

    if current is None:
        store.add(entry)
        return

    updates: dict[str, Any] = {"workflow_id": workflow_id, "action": "workflow_run", "prompt": ""}
    for field in _TIMING_FIELDS:
        value = getattr(entry, field)
        updates[field] = value.model_dump() if isinstance(value, ActiveHours) else value
    store.update(schedule_id, updates, context=config.validation_context())


# --- workflow-store access (deployment-wide, binding-independent) ----------

# Test seam: overridden to inject a fake definition store. Production leaves it
# None and reads the real deployment store off disk.
_definitions_factory: Callable[[], Any] | None = None


def _definitions() -> Any:
    """A read-only handle on the deployment's workflow bundles, or ``None``.

    Built directly from the shared bundle root (``workflows_dir()``) rather than
    through the workflows module's per-agent runtime, deliberately: the backfill
    runs at capability-setup time, where that module's ``ContextVar`` may not yet
    be bound in this task — and the bundle store is deployment-wide, not per
    agent, so the direct read is both correct and immune to that timing. ``None``
    means this agent has no fleet, or its fleet has no workflow engine
    installed: either way there is nothing to reconcile.
    """
    if _definitions_factory is not None:
        return _definitions_factory()
    fleet = _runtime.state().fleet
    if fleet is None:
        return None
    opener = getattr(fleet, "open_definitions", None)
    return opener() if callable(opener) else None


def _owner_of(bundle: Any) -> str:
    """The bundle's owning agent handle, without the leading ``@``."""
    return str(getattr(bundle.definition, "owner", "")).lstrip("@")


def _owned_triggers(definitions: Any, agent_name: str) -> dict[str, Any]:
    """Every workflow THIS agent owns → its effective trigger.

    Ownership scoping is what keeps a deployment-wide workflow from being
    scheduled by all six agents and firing six times a night: exactly one
    agent — the owner — materialises its schedule. Archived owned workflows come
    through with a ``None`` trigger, so ``full_reconcile`` prunes their entries.
    """
    triggers: dict[str, Any] = {}
    if not agent_name:
        return triggers
    for workflow_id in definitions.list_ids(include_archived=True):
        try:
            bundle = definitions.load(workflow_id)
        except Exception:  # reason: one unreadable bundle must not stall the rest
            _logger.warning("could not load workflow '%s' for schedule sync", workflow_id)
            continue
        if _owner_of(bundle) == agent_name:
            triggers[workflow_id] = bundle.effective_trigger
    return triggers


async def reconcile_workflow_schedules() -> None:
    """Backfill this agent's schedule store from the workflows it owns. Best-effort."""
    try:
        definitions = _definitions()
        if definitions is None:
            return
        state = _runtime.state()
        triggers = _owned_triggers(definitions, state.agent_name)
        full_reconcile(state.store, state.config, triggers, anchor=_now())
        _logger.info("Reconciled %d owned workflow trigger(s) into the scheduler", len(triggers))
    except Exception:  # reason: a sync failure must never break scheduler startup
        _logger.warning("workflow-trigger schedule reconciliation failed", exc_info=True)


async def sync_workflow_schedule(workflow_id: str) -> None:
    """Push one owned workflow's trigger into the scheduler store. Best-effort."""
    try:
        definitions = _definitions()
        if definitions is None or not definitions.exists(workflow_id):
            return
        state = _runtime.state()
        bundle = definitions.load(workflow_id)
        if _owner_of(bundle) != state.agent_name:
            return  # only the owner schedules its workflow
        push_one(state.store, state.config, workflow_id, bundle.effective_trigger, anchor=_now())
    except Exception:  # reason: a sync failure must never break the builder tool
        _logger.warning("could not sync a schedule for workflow '%s'", workflow_id, exc_info=True)


__all__ = [
    "DERIVED_ID_PREFIX",
    "derived_id",
    "desired_entry",
    "full_reconcile",
    "parse_cron_tz",
    "push_one",
    "reconcile_workflow_schedules",
    "sync_workflow_schedule",
]
