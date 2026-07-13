"""Decorator-form tasks module — SPEC-056 Phase B.

Ten module-level ``@tool`` functions expose the mission-control task
surface over the arcstore-backed ``TaskStore`` (Phase A). There is no
``@capability`` class (unlike the scheduler template's ``SchedulerEngine``).

Phase D adds an opt-in dispatch loop (``@background_task``): when
``config.dispatch`` is on, each tick pulls the agent's highest-priority
ready ``todo`` task — whether a teammate's ``assign_task`` or an arcui board
assignment put it there — starts it, and wakes a real agent run via the
``agent_run_fn`` bound at ``agent:ready`` (the same seam the messaging
module uses). Reacting to the durable arcstore owner write is what makes the
two assignment surfaces uniform: arcui runs in a separate process and cannot
sign an inter-agent envelope, so a poll of the shared store — not a pushed
message — is the only mechanism that covers a board assignment.

Audit is emitted CENTRALLY by the tool registry, keyed on each tool's
declared ``classification`` (SDD §3, deepen correction) — tools declare
classification, they never call ``arctrust.audit.emit`` themselves. Free
text (title/description) is sanitized by the arcstore ``Task`` model's own
field validator at construction (LLM01/ASI06), so an injection payload
raises ``ValidationError`` (a ``ValueError`` subclass) the tool catches and
returns as a clean ``{"error"}``. Owner-only mutation is gated by
:func:`_require_owner`, checked against the runtime state's ``identity``
before every state transition (create is exempt — there is no prior owner
to protect; assign is exempt — reassignment is not the owner's call, SDD
§3). Runtime state is fetched via :func:`_state`, not
``_runtime.state()`` directly — the module's async wiring (opening the
arcstore backend, and the live registry) is deferred and finished there on
first use, since ``_runtime.configure()`` itself is sync (see
``_runtime``'s module docstring for why).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from arcteam.registry import resolve
from arcteam.types import Entity, EntityStatus, EntityType, Message, MsgType

from arcagent.modules.tasks import _runtime
from arcagent.modules.tasks.models import Priority, Task
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.sanitizer import sanitize_text

# A SQLite lock-timeout under shared-db contention surfaces as
# ``sqlite3.OperationalError`` from deep in the store; catch it alongside the
# validation errors so a tool degrades to a clean ``{"error"}`` (REL-F3b).
_TOOL_ERRORS = (ValueError, TypeError, sqlite3.OperationalError)

_logger = logging.getLogger("arcagent.modules.tasks.capabilities")


async def _state() -> _runtime._State:
    """Fetch runtime state, finishing the module's lazy async wiring first.

    ``_runtime.configure()`` is sync (mirrors every other module — the
    dispatcher calls it without ``await``), so the SQLite backend and, when
    live, the registry are opened lazily by ``ensure_store()`` on first tool
    use rather than at configure time.
    """
    await _runtime.ensure_store()
    return _runtime.state()


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


async def _resolve_owner(owner: str | None, st: _runtime._State) -> str | None:
    """Resolve the ``owner`` tool argument to a DID.

    ``None`` (argument omitted) defaults to self; ``""`` leaves the task
    unowned (backlog); anything else is an address ref (``@handle``,
    ``did:...``) resolved via arcteam — unavailable if no registry could be
    injected or built live (SDD §3).
    """
    if owner is None:
        self_did: str = st.identity.did
        return self_did
    if owner == "":
        return None
    if st.registry is None:
        msg = "registry unavailable"
        raise ValueError(msg)
    return await resolve(st.registry, owner)


def _require_owner(task: Task, st: _runtime._State) -> None:
    """Raise ``ValueError`` unless ``task`` is unowned or owned by this agent.

    Applied before every mutation except create (no prior owner to protect)
    and assign (reassignment is not the owner's call — SDD §3/§7).
    """
    if task.owner_did is not None and task.owner_did != st.identity.did:
        msg = f"Task '{task.id}' is owned by another agent"
        raise ValueError(msg)


@tool(
    name="create_task",
    description="Create a task, owned by self (default), a teammate (@handle), or unowned",
    classification="state_modifying",
)
async def create_task(
    title: str = "",
    description: str = "",
    priority: str = "medium",
    owner: str | None = None,
    blocked_by: list[str] | None = None,
) -> str:
    st = await _state()
    try:
        owner_did = await _resolve_owner(owner, st)
        task_id = _new_task_id()
        deps = blocked_by or []
        # Reject a dependency cycle up front (P2): a cyclic task could never
        # become ready, so it must never be written (ASI08 — no unsatisfiable
        # work in the DAG). A brand-new id can only cycle via a self-edge here,
        # but the guard is the single enforcement point for every blocked_by.
        if await st.store.deps_would_cycle(task_id, deps):
            return json.dumps({"error": "blocked_by would create a dependency cycle"})
        task = Task(
            id=task_id,
            title=title,
            description=description,
            priority=priority,  # type: ignore[arg-type] # validated by Task's Literal on construction
            owner_did=owner_did,
            creator_did=st.identity.did,
            blocked_by=deps,
            # Seed the retry ceiling from config so this agent's created tasks
            # honor the deployment default; the per-task field is authoritative
            # thereafter (the reliability engine reads task.max_attempts).
            max_attempts=st.config.default_max_attempts,
        )
        created = await st.store.create(task)
        return str(created.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="update_task",
    description="Update title/description/priority on an owned, at-rest task",
    classification="state_modifying",
)
async def update_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
) -> str:
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        candidates: dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
        }
        updates = {k: v for k, v in candidates.items() if v is not None}
        updated = await st.store.update(id, updates, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="start_task",
    description="Start an owned (or unowned) task; unowned tasks are claimed by self",
    classification="state_modifying",
)
async def start_task(id: str = "") -> str:  # noqa: A002 - matches JSON schema field name
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        task, reason = await st.store.start_task(id, st.identity.did)
        if task is None:
            return json.dumps({"error": f"unable to start task '{id}' ({reason})"})
        return json.dumps({"reason": reason, "task": task.model_dump(mode="json")})
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="complete_task",
    description="Mark an owned task done, with resolution and optional structured output",
    classification="state_modifying",
)
async def complete_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    resolution: str = "",
    output: dict[str, Any] | None = None,
) -> str:
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        if not await st.store.deps_met(current):
            return json.dumps(
                {"error": f"Task '{id}' is blocked by unfinished dependencies"}
            )
        if current.requires_review:
            # Opt-in human gate (P3): land in ``review``, not ``done`` — an
            # operator approves/rejects before it is terminal (LLM06/ASI09).
            updates: dict[str, Any] = {"status": "review", "resolution": resolution}
            if output is not None:
                updates["output"] = output
            updated = await st.store.update(id, updates, actor_did=st.identity.did)
            await _notify_operator(st, f"needs review: {current.title}", current.classification)
        else:
            updated = await st.store.finish(
                id, status="done", resolution=resolution, output=output, actor_did=st.identity.did
            )
            await _notify_operator(st, f"done: {current.title}", current.classification)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="fail_task",
    description="Mark an owned task failed, with a short resolution",
    classification="state_modifying",
)
async def fail_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    resolution: str = "",
) -> str:
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        updated = await st.store.finish(
            id, status="failed", resolution=resolution, actor_did=st.identity.did
        )
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        await _notify_operator(st, f"failed: {current.title}", current.classification, alert=True)
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="assign_task",
    description="Reassign an at-rest task to a teammate (@handle); rejects in-progress tasks",
    classification="state_modifying",
)
async def assign_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    to_handle: str = "",
) -> str:
    st = await _state()
    if st.registry is None:
        return json.dumps({"error": "registry unavailable"})
    try:
        to_did = await resolve(st.registry, to_handle)
        updated = await st.store.assign(id, to_did, st.identity.did)
        if updated is None:
            return json.dumps({"error": f"unable to assign task '{id}'"})
        await _notify_assignee(st, to_handle, updated)
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


async def _notify_assignee(st: _runtime._State, to_handle: str, task: Task) -> None:
    """Send a ``TASK_ASSIGNED`` hand-off to the assignee's inbox (SDD §5).

    Best-effort: the arcstore owner write above is already durable truth, so
    a delivery failure here (unreachable bus, unregistered sender, etc.) is
    logged and swallowed rather than surfaced to the caller — assign_task
    must not roll back or mask a successful write just because notification
    could not go out.
    """
    if st.messenger is None:
        return
    handle = to_handle.removeprefix("@")
    # Carry the task's classification onto the envelope (SEC-F3) so the
    # messenger's no-write-down check engages — an UNCLASSIFIED default would
    # leave it inert regardless of how sensitive the task is (ASI07).
    message = Message(
        sender=st.identity.did,
        to=[f"agent://{handle}"],
        msg_type=MsgType.TASK_ASSIGNED,
        body=f"@{handle} task_id={task.id} — {task.title}",
        classification=task.classification,
    )
    try:
        await st.messenger.send(message)
    except Exception:
        _logger.warning("failed to notify @%s of assignment for task '%s'", handle, task.id)


async def _notify_operator(
    st: _runtime._State, body: str, classification: str, *, alert: bool = False
) -> None:
    """Best-effort operator notification on a key task transition (P4).

    Sends to ``user://operator`` (trifecta-allowed). Gated by ``config.notify``
    and the presence of a live messenger; a delivery failure is logged and
    swallowed so it can NEVER block or roll back the transition that triggered
    it (the store write is already durable truth, and AU has recorded it).
    ``alert`` picks ``ALERT`` (failures/escalations) over ``INFO``.
    """
    if not st.config.notify or st.messenger is None:
        return
    message = Message(
        sender=st.identity.did,
        to=["user://operator"],
        msg_type=MsgType.ALERT if alert else MsgType.INFO,
        body=body,
        classification=classification,
    )
    try:
        await st.messenger.send(message)
    except Exception:
        _logger.warning("failed to notify operator: %s", body[:80])


@tool(
    name="claim_task",
    description="Pull the next available task for self, respecting the in-progress cap",
    classification="state_modifying",
)
async def claim_task() -> str:
    st = await _state()
    try:
        task, reason = await st.store.claim_next(st.identity.did)
        return json.dumps(
            {"reason": reason, "task": task.model_dump(mode="json") if task else None}
        )
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="list_tasks",
    description="List tasks scoped to self or the whole team, optionally filtered by status",
    classification="read_only",
)
async def list_tasks(scope: str = "team", status: str | None = None) -> str:
    st = await _state()
    owner_did = st.identity.did if scope == "self" else None
    try:
        tasks = await st.store.list(status=status, owner_did=owner_did)
        return json.dumps([t.model_dump(mode="json") for t in tasks])
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="decompose_task",
    description="Break an owned task into sub-tasks; the parent becomes blocked_by them (FR-15)",
    classification="state_modifying",
)
async def decompose_task(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    subtasks: list[dict[str, Any]] | None = None,
) -> str:
    st = await _state()
    try:
        parent = await st.store.get(id)
        if parent is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(parent, st)
        # Build ALL subtasks before persisting any, so a bad title in a later
        # subtask (rejected by the Task model's field validator on construction)
        # can't leave earlier ones orphaned (no partial write).
        subs: list[Task] = []
        for spec in subtasks or []:
            subs.append(
                Task(
                    id=_new_task_id(),
                    title=spec.get("title", ""),
                    description=spec.get("description", ""),
                    priority=spec.get("priority", "medium"),
                    owner_did=st.identity.did,
                    creator_did=st.identity.did,
                    parent_id=id,
                )
            )
        # Cycle guard BEFORE any write (P2), so a rejected decompose leaves no
        # orphan children. Fresh leaf subtasks can't close a loop, but this keeps
        # every blocked_by mutation flowing through the one acyclicity check.
        proposed_blocked_by = [*parent.blocked_by, *(s.id for s in subs)]
        if await st.store.deps_would_cycle(id, proposed_blocked_by):
            return json.dumps({"error": "decompose would create a dependency cycle"})
        created_subs = [await st.store.create(sub) for sub in subs]
        updated_parent = await st.store.update(
            id, {"blocked_by": proposed_blocked_by}, actor_did=st.identity.did
        )
        if updated_parent is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return json.dumps(
            {
                "parent": updated_parent.model_dump(mode="json"),
                "subtasks": [s.model_dump(mode="json") for s in created_subs],
            }
        )
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


@tool(
    name="set_task_output",
    description="Attach a structured result ({summary, artifacts}) to an owned task (FR-13)",
    classification="state_modifying",
)
async def set_task_output(
    id: str = "",  # noqa: A002 - matches JSON schema field name
    output: dict[str, Any] | None = None,
) -> str:
    st = await _state()
    try:
        current = await st.store.get(id)
        if current is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        _require_owner(current, st)
        updated = await st.store.update(id, {"output": output or {}}, actor_did=st.identity.did)
        if updated is None:
            return json.dumps({"error": f"Task '{id}' not found"})
        return str(updated.model_dump_json())
    except _TOOL_ERRORS as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Dispatch loop (SPEC-056 Phase D) — assigned tasks actually run
# ---------------------------------------------------------------------------

# Poll cadence for the dispatch loop. A fixed decorator arg (interval cannot
# read per-agent config at decoration time); the real on/off switch is
# ``config.dispatch``, checked inside each tick. 15s is responsive enough for
# an operator assigning work from the board without hammering the shared DB.
_DISPATCH_TICK = 15.0

# Faster cadence for the reliability watcher (cancel + stuck-reclaim). It only
# reads the agent's own in_progress tasks and cancels in-memory handles, so it
# is cheap; a short interval makes operator "stop" feel responsive.
_RELIABILITY_TICK = 5.0

# Session key prefix for a dispatched task's run — one transcript per task so
# the board and the session log line up (``<workspace>/sessions/task:<id>.jsonl``).
_TASK_SESSION = "task"

# Highest-priority-first ordering (mirrors arcstore's claim order, SDD §2).
_PRIORITY_RANK: dict[Priority, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _format_task_prompt(task: Task) -> str:
    """Render the run prompt handed to the agent for an assigned task.

    Title/description are re-sanitized before prompt interpolation (LLM01/
    ASI06 defence-in-depth) even though the arcstore ``Task`` validator already
    scrubbed them at write time — the prompt is an instruction surface.
    """
    title = sanitize_text(task.title, max_length=500)
    lines = [
        f"You have been assigned task {task.id}.",
        f"Title: {title}",
    ]
    if task.description:
        lines.append(f"Details: {sanitize_text(task.description, max_length=4000)}")
    lines.append(f"Priority: {task.priority}")
    lines.append(
        "Do the work now. When finished, call complete_task with a short "
        "resolution — or fail_task if you cannot complete it. Work silently; "
        "only notify the user for a meaningful result, a question, or a blocker."
    )
    return "\n".join(lines)


def _resolve_timeout(task: Task, config: Any) -> float | None:
    """The wall-clock cap for this run: per-task override, else config default.

    ``None`` means unbounded (both knobs 0/None). Kept a pure function so the
    timeout policy is testable without a running loop.
    """
    timeout = task.timeout_seconds if task.timeout_seconds else config.task_timeout_seconds
    return timeout if timeout and timeout > 0 else None


def _backoff_elapsed(task: Task, now: str) -> bool:
    """True if a task's retry backoff has elapsed (or it has none).

    ``next_attempt_at`` and ``now`` are both ``_now()``-formatted UTC ISO
    strings, so a lexicographic compare is chronological.
    """
    return task.next_attempt_at is None or task.next_attempt_at <= now


async def _is_dispatchable(st: _runtime._State, task: Task, now: str) -> bool:
    """Whether a todo task should be started now (P1 gates + P2 DAG gates).

    Skips a task whose retry backoff has not elapsed, whose dependencies aren't
    all done, or that has subtasks — a coordinator parent auto-completes from
    its children (reconcile), it never runs.
    """
    if not _backoff_elapsed(task, now):
        return False
    if not await st.store.deps_met(task):
        return False
    return not await st.store.children(task.id)


async def _dispatch_tick() -> None:
    """Run one poll-and-dispatch tick. Factored out so it is directly testable.

    No-op unless dispatch is enabled AND a run callback is bound. Respects the
    one-``in_progress``-task cap (never stacks a second run), skips tasks whose
    dependencies are unmet or whose retry backoff has not elapsed, picks the
    highest-priority ready task, starts it (todo -> in_progress), and runs it
    under the reliability wrapper (timeout + retry/dead-letter).
    """
    st = await _state()
    if not st.config.dispatch or st.agent_run_fn is None:
        return
    self_did = st.identity.did
    # Cap guard: if a task is already running for this agent, leave it be.
    if await st.store.list(status="in_progress", owner_did=self_did):
        return
    todos = await st.store.list(status="todo", owner_did=self_did)
    now = datetime.now(UTC).isoformat()
    ready = [t for t in todos if await _is_dispatchable(st, t, now)]
    if not ready:
        return
    ready.sort(key=lambda t: (_PRIORITY_RANK.get(t.priority, 99), t.created_at or ""))
    picked = ready[0]
    # Pin the run id up front and stamp it in the same atomic write that claims
    # the task, then hand the SAME id to the run so the loop's spooled events
    # share it — the arcui activity timeline joins task.run_id to those events.
    run_id = str(uuid.uuid4())
    started, _reason = await st.store.start_task(picked.id, self_did, run_id=run_id)
    if started is None or started.status != "in_progress":
        # Lost the atomic claim (a concurrent starter won) — try again next tick.
        return
    await _run_task(st, started, run_id, self_did)


async def _run_task(st: _runtime._State, task: Task, run_id: str, self_did: str) -> None:
    """Drive one dispatched run under the reliability wrapper (P1).

    The run is a tracked ``asyncio.Task`` (so the watcher can cancel it) wrapped
    in an optional wall-clock timeout. A normal return leaves the task's status
    as the agent set it (done/failed via its own tools, or in_progress if it
    never completed — the stuck-reclaim path handles that later). A timeout or
    unhandled error is a failed attempt fed to the retry engine; an operator
    cancel (the watcher cancelled the run, recorded in ``st.cancelling``) is a
    terminal dead-letter — process shutdown re-raises instead.
    """
    timeout = _resolve_timeout(task, st.config)
    run = asyncio.ensure_future(
        st.agent_run_fn(
            _format_task_prompt(task),
            session_key=f"{_TASK_SESSION}:{task.id}",
            run_id=run_id,
        )
    )
    st.running[task.id] = run
    try:
        await asyncio.wait_for(run, timeout)
    except TimeoutError:
        await _handle_attempt_failure(st, task.id, self_did, f"timeout after {timeout:g}s")
    except asyncio.CancelledError:
        if task.id in st.cancelling:
            st.cancelling.discard(task.id)
            await st.store.dead_letter(
                task.id, actor_did=self_did, resolution="cancelled", last_error="cancelled"
            )
        else:
            raise  # genuine shutdown — never swallow the loop's own cancellation
    except Exception as exc:  # reason: any run failure feeds the retry engine (LLM10/ASI08)
        await _handle_attempt_failure(st, task.id, self_did, f"{type(exc).__name__}: {exc}")
    finally:
        st.running.pop(task.id, None)


async def _handle_attempt_failure(
    st: _runtime._State, task_id: str, self_did: str, error: str
) -> None:
    """Retry (with exponential backoff) or dead-letter a failed attempt (P1).

    ``attempts`` was incremented at start, so it is the count of tries so far.
    Below the ceiling -> requeue to ``todo`` gated by an exponential backoff;
    at/above it -> terminal ``failed`` (dead letter). Both writes are status-
    conditional in the store, so a concurrent stuck-reclaim can't double-apply.
    """
    current = await st.store.get(task_id)
    if current is None:
        return
    error = sanitize_text(error, max_length=500)
    if current.attempts >= current.max_attempts:
        await st.store.dead_letter(
            task_id,
            actor_did=self_did,
            resolution=f"failed after {current.attempts} attempt(s) — retries exhausted",
            last_error=error,
        )
        await _notify_operator(
            st, f"dead-lettered: {current.title} ({error})", current.classification, alert=True
        )
        return
    backoff = st.config.retry_backoff_seconds * (2 ** (current.attempts - 1))
    next_at = (datetime.now(UTC) + timedelta(seconds=backoff)).isoformat()
    await st.store.requeue(
        task_id, actor_did=self_did, last_error=error, next_attempt_at=next_at
    )


def _is_stale(task: Task, now: datetime, threshold: float) -> bool:
    """True if an in_progress task's run looks dead (no progress past threshold).

    A missing/unparseable ``started_at`` on an in_progress task is itself
    anomalous, so treat it as stale (reclaim it).
    """
    if not task.started_at:
        return True
    try:
        started = datetime.fromisoformat(task.started_at)
    except ValueError:
        return True
    return (now - started).total_seconds() >= threshold


async def _reliability_tick() -> None:
    """One cancel + stuck-reclaim pass over the agent's in_progress tasks (P1).

    For each in_progress task this agent owns: an operator cancel request stops
    the live run (or dead-letters it directly if no run is live); otherwise, a
    task with no live run is reclaimed as a failed attempt — immediately on the
    first pass (pre-restart orphans) or once past ``stuck_reclaim_seconds``
    thereafter (a run that ended without completing). A live, healthy run is
    left untouched.
    """
    st = await _state()
    if not st.config.dispatch:
        return
    self_did = st.identity.did
    in_progress = await st.store.list(status="in_progress", owner_did=self_did)
    now = datetime.now(UTC)
    first_pass = not st.reclaim_done
    for task in in_progress:
        if task.cancel_requested:
            await _cancel_running(st, task, self_did)
        elif _should_reclaim(st, task, now, first_pass):
            await _notify_operator(
                st,
                f"escalation — stuck task reclaimed: {task.title}",
                task.classification,
                alert=True,
            )
            await _handle_attempt_failure(
                st, task.id, self_did, "stuck: no active run — reclaimed"
            )
    st.reclaim_done = True
    await _reconcile_parents(st, self_did)
    await _route_unassigned(st, self_did)


async def _reconcile_parents(st: _runtime._State, self_did: str) -> None:
    """Roll a decomposition parent up from its children's terminal states (P2).

    A parent this agent owns that is still open and has subtasks auto-completes
    when every child is ``done``, and auto-fails the moment any child fails
    terminally (a failed subtask makes the parent unachievable — fail-fast,
    ASI08). Runs each reliability tick so multi-level DAGs settle bottom-up over
    successive passes. The transition is deterministic and audited (the store
    write's resolution records why).
    """
    # One list + in-memory grouping (no children() query per task). Children of
    # a decomposition are created owned by the same agent as the parent, so
    # grouping this agent's own tasks by parent_id sees the whole family;
    # cross-owner children (post-reassignment, a later phase) are out of scope.
    mine = await st.store.list(owner_did=self_did)
    children_by_parent: dict[str, list[Task]] = {}
    for task in mine:
        if task.parent_id:
            children_by_parent.setdefault(task.parent_id, []).append(task)
    for parent in mine:
        if parent.status in ("done", "failed"):
            continue
        children = children_by_parent.get(parent.id)
        if children:
            await _reconcile_one_parent(st, parent, children, self_did)


async def _reconcile_one_parent(
    st: _runtime._State, parent: Task, children: list[Task], self_did: str
) -> None:
    """Apply the roll-up rule to one open parent (fail-fast, else all-done)."""
    if any(c.status == "failed" for c in children):
        await st.store.finish(
            parent.id,
            status="failed",
            resolution="a subtask failed — parent cannot complete",
            actor_did=self_did,
        )
    elif all(c.status == "done" for c in children) and await st.store.deps_met(parent):
        # Require the parent's FULL dependency set, not just its children — a
        # parent that also carries non-child blocked_by deps waits for those too.
        await st.store.finish(
            parent.id,
            status="done",
            resolution="all subtasks complete",
            actor_did=self_did,
        )


# ---------------------------------------------------------------------------
# Auto-routing (SPEC-056 Phase 3) — ownerless tasks find the best agent
# ---------------------------------------------------------------------------


async def _route_unassigned(st: _runtime._State, self_did: str) -> None:
    """Route every ownerless task to the best eligible agent (P3).

    No-op without routing enabled or a live registry (the roster source).
    Selection is deterministic (see :func:`_pick_agent`) so concurrent routers
    on different agents converge; the store's ``route`` is owner-null-conditional
    so exactly one write lands. Load is tracked incrementally across the pass so
    a burst of tasks spreads rather than piling on the momentary least-loaded.
    """
    if not st.config.routing or st.registry is None:
        return
    unassigned = await st.store.unassigned()
    if not unassigned:
        return
    agents = await _eligible_agents(st)
    if not agents:
        return
    load = await _load_by_owner(st)
    for task in unassigned:
        chosen = _pick_agent(task, agents, load)
        routed = await st.store.route(task.id, chosen.did, self_did)
        if routed is not None:
            load[chosen.did] = load.get(chosen.did, 0) + 1
            await _notify_assignee(st, chosen.handle, routed)


async def _eligible_agents(st: _runtime._State) -> list[Entity]:
    """Active agent entities from the registry — the routing candidate set."""
    entities = await st.registry.list_entities()
    return [
        e for e in entities if e.type == EntityType.AGENT and e.status == EntityStatus.active
    ]


async def _load_by_owner(st: _runtime._State) -> dict[str, int]:
    """Current in-flight load per agent: count of todo + in_progress tasks."""
    load: dict[str, int] = {}
    for status in ("todo", "in_progress"):
        for task in await st.store.list(status=status):
            if task.owner_did:
                load[task.owner_did] = load.get(task.owner_did, 0) + 1
    return load


def _pick_agent(task: Task, agents: list[Entity], load: dict[str, int]) -> Entity:
    """Least-loaded eligible agent, preferring a capability match, tie-break name.

    A capability match (task tag ∈ agent capabilities) dominates load, so a
    matching agent is chosen even if busier; among equals, least-loaded then
    name. Goal-relevance routing would slot in here as a richer score — the seam.
    """

    def rank(agent: Entity) -> tuple[int, int, str]:
        matches = bool(set(task.tags) & set(agent.capabilities))
        return (0 if matches else 1, load.get(agent.did, 0), agent.name)

    return min(agents, key=rank)


def _should_reclaim(st: _runtime._State, task: Task, now: datetime, first_pass: bool) -> bool:
    """Whether an in_progress task with no live run should be reclaimed now.

    Never reclaim a task with a live run. On the first pass reclaim any orphan
    (a pre-restart in_progress task); thereafter only once it is past the
    staleness threshold (a run that ended without ever completing).
    """
    if task.id in st.running:
        return False
    return first_pass or _is_stale(task, now, st.config.stuck_reclaim_seconds)


async def _cancel_running(st: _runtime._State, task: Task, self_did: str) -> None:
    """Honor a cancel request: stop the live run, or dead-letter if none.

    A live run is cancelled via its tracked handle; ``st.cancelling`` marks the
    stop as deliberate so :func:`_run_task` finalizes it as cancelled (not a
    shutdown re-raise). With no live run (e.g. the process restarted after the
    request), finalize the task directly.
    """
    run = st.running.get(task.id)
    if run is not None:
        st.cancelling.add(task.id)
        run.cancel()
        return
    await st.store.dead_letter(
        task.id, actor_did=self_did, resolution="cancelled", last_error="cancelled"
    )


@hook(event="agent:ready", priority=100)
async def tasks_bind_run_fn(ctx: Any) -> None:
    """Bind the agent's run callback for the dispatch loop (mirrors messaging).

    ``run_fn`` (``ArcAgent.run_collected``) is delivered on the ``agent:ready``
    payload; the dispatch loop needs it to actually run an assigned task.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    run_fn = data.get("run_fn")
    if run_fn is not None:
        _runtime.state().agent_run_fn = run_fn


@background_task(name="tasks_dispatch_loop", interval=_DISPATCH_TICK)
async def tasks_dispatch_loop(_ctx: Any) -> None:
    """Background loop: pull and run the agent's ready assigned tasks.

    The loader spawns this once and it owns its own cadence (mirrors the memory
    consolidate + skills curator loops — ``register_task`` calls ``fn(None)`` a
    single time, so the ``while True`` MUST live here or the loop runs one tick
    and dies). Gated by ``config.dispatch`` inside :func:`_dispatch_tick`. The
    awaited run inside each tick keeps dispatch serial — a tick does not return
    until the current task's run does — so the in-progress cap holds without
    extra locking.
    """
    while True:
        try:
            await _dispatch_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a tick error must never crash the agent
            _logger.warning("tasks dispatch tick failed", exc_info=True)
        await asyncio.sleep(_DISPATCH_TICK)


@background_task(name="tasks_reliability_watcher", interval=_RELIABILITY_TICK)
async def tasks_reliability_watcher(_ctx: Any) -> None:
    """Background loop: honor operator cancels and reclaim stuck runs.

    Owns its own cadence (see :func:`tasks_dispatch_loop`). Runs concurrently
    with (and faster than) the dispatch loop so an operator "stop" reaches a run
    while the dispatch tick is still awaiting it, and so a task orphaned
    in_progress by a crash/restart is recovered rather than stranded. Gated by
    ``config.dispatch`` inside :func:`_reliability_tick`.
    """
    while True:
        try:
            await _reliability_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a tick error must never crash the agent
            _logger.warning("tasks reliability tick failed", exc_info=True)
        await asyncio.sleep(_RELIABILITY_TICK)


__all__ = [
    "assign_task",
    "claim_task",
    "complete_task",
    "create_task",
    "decompose_task",
    "fail_task",
    "list_tasks",
    "set_task_output",
    "start_task",
    "tasks_bind_run_fn",
    "tasks_dispatch_loop",
    "tasks_reliability_watcher",
    "update_task",
]
