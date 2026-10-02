"""Production store adapters binding the runner to arcstore (COMP-006/COMP-007).

The runner speaks the narrow contracts in :mod:`runner_contracts`; arcstore
owns the durable plane. These adapters are the join, and they exist rather than
the runner importing arcstore's models directly so the engine stays testable
against doubles and arcstore stays free to evolve its own shapes.

Two facts about the split, because they are not obvious:

**The Run row and the runner's journal are different records.** ``arcstore``'s
``PathEntry`` records a node's TERMINAL OUTCOME (``done | failed | skipped``) —
it is the trace a dashboard renders. The runner additionally needs bookkeeping
that has no outcome at all: which nodes it has already materialized, which
settlements it has already counted, which loop back-edges it has already
followed. That is idempotency state, not a trace, so it lives in a companion
row this adapter owns. The canonical Run stays the one everybody else reads.

**Skips and router choices only exist here.** An untaken branch never becomes a
task row (lazy materialization), so the Run's path is the ONLY record that a
branch was considered and not taken. Node outcomes for nodes that did run are
read from their task rows; the Run's trace carries what the task rows cannot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from arcstore.mutation_fence import RunnerFence
from arcstore.runs import NodeState, PathEntry, Run, RunStore
from arcstore.tasks import Task, TaskStore, _validate_free_text, reclaim_allowance_s
from arctrust import sanitize_error_text
from arctrust.audit import AuditSink

from .narrator import RunNarrator
from .runner_contracts import TERMINAL_RUN_STATUSES, RunStatus

logger = logging.getLogger(__name__)


class RunStateMissingError(RuntimeError):
    """The runner's companion state row for a run is gone."""


_STATE_COLLECTION = "workflow_run_state"
_TASK_COLLECTION = "tasks"
_CAS_RETRIES = 32

# Path-entry kinds that describe something no task row can show.
_TRACEABLE_OUTCOMES: dict[str, str] = {"skipped": "skipped", "gate": "done"}


@dataclass(frozen=True)
class FlowRun:
    """A Run as the runner needs it: the durable row plus the runner's own state."""

    run_id: str
    workflow_id: str
    version: int
    content_hash: str
    status: RunStatus
    initiator_did: str
    channel: str | None
    input: Mapping[str, Any]
    path_taken: Sequence[Mapping[str, Any]]
    budget_tokens: int | None
    budget_cost_usd: float | None
    budget_wall_clock_s: float | None
    tokens_spent: int
    cost_spent: float
    started_at: str | None
    resolution: str | None = None
    last_error: str | None = None
    node_states: Mapping[str, NodeState] = field(default_factory=dict)
    revision: int = 0


@dataclass
class _MutablePlane:
    """The backend primitives these adapters need."""

    backend: Any
    sink: AuditSink | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class WorkflowRunStore:
    """``RunStoreLike`` over ``arcstore.runs.RunStore`` plus a companion row."""

    def __init__(self, backend: Any, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._runs = RunStore(backend, sink=sink)
        self._sink = sink

    async def create_run(
        self,
        *,
        run_id: str,
        workflow_id: str,
        version: int,
        content_hash: str,
        trigger_digest: str | None = None,
        initiator_did: str,
        channel: str | None,
        input: Mapping[str, Any],  # noqa: A002 — the definition's own vocabulary
        budget_tokens: int | None,
        budget_cost_usd: float | None,
        budget_wall_clock_s: float | None,
        fence: RunnerFence | None = None,
    ) -> tuple[FlowRun, bool]:
        """Create the run, or return the one that exists. ``created`` says who won.

        The companion row is insert-if-absent, so the creation token it holds
        is the arbiter: only the caller whose token survived is the creator.
        Two racing first starts therefore cannot both announce the run.
        """
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "run_id": run_id,
                    "workflow_id": workflow_id,
                    "version": version,
                    "content_hash": content_hash,
                    "trigger_digest": trigger_digest,
                    "initiator_did": initiator_did,
                    "channel": channel,
                    "input": dict(input),
                    "budget_tokens": budget_tokens,
                    "budget_cost_usd": budget_cost_usd,
                    "budget_wall_clock_s": budget_wall_clock_s,
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        await self._runs.create(
            Run(
                id=run_id,
                workflow_id=workflow_id,
                workflow_version=version,
                content_hash=content_hash,
                request_digest=request_digest,
                status="running",
                initiator_did=initiator_did,
            ),
            fence=fence,
        )
        creation_token = uuid4().hex
        state: dict[str, Any] = {
            "run_id": run_id,
            "request_digest": request_digest,
            "creation_token": creation_token,
            "trigger_digest": trigger_digest,
            "channel": channel,
            "input": dict(input),
            "budget_tokens": budget_tokens,
            "budget_cost_usd": budget_cost_usd,
            "budget_wall_clock_s": budget_wall_clock_s,
            "path": [],
            "path_len": 0,
            "resolution": None,
            "advance_failure_count": 0,
            "advance_failure_basis": "running:0",
            "advance_failure_revision": 0,
        }
        rows = await self._backend.mutable_create_batch(
            _STATE_COLLECTION,
            [(run_id, state)],
            actor_did=initiator_did,
            sink=self._sink,
            fence=fence,
        )
        if rows[0].get("request_digest") != request_digest:
            raise ValueError("workflow companion identity already exists with a different request")
        loaded = await self.get(run_id)
        if loaded is None:  # pragma: no cover — the row was just written
            raise RuntimeError(f"run {run_id} vanished immediately after creation")
        return loaded, rows[0].get("creation_token") == creation_token

    async def get(self, run_id: str) -> FlowRun | None:
        run = await self._runs.get(run_id)
        if run is None:
            return None
        state = await self._backend.mutable_read(_STATE_COLLECTION, run_id) or {}
        return FlowRun(
            run_id=run.id,
            workflow_id=run.workflow_id,
            version=run.workflow_version,
            content_hash=run.content_hash,
            status=run.status,
            initiator_did=run.initiator_did,
            channel=state.get("channel"),
            input=state.get("input") or {},
            path_taken=state.get("path") or [],
            budget_tokens=state.get("budget_tokens"),
            budget_cost_usd=state.get("budget_cost_usd"),
            budget_wall_clock_s=state.get("budget_wall_clock_s"),
            tokens_spent=run.budget.tokens_spent,
            cost_spent=0.0,
            started_at=run.created_at,
            resolution=state.get("resolution"),
            last_error=run.last_error,
            node_states=dict(run.node_states),
            revision=run.revision,
        )

    async def active_runs(self) -> Sequence[FlowRun]:
        """Every run the tick should advance — scoped by status, never listed whole."""
        active: list[FlowRun] = []
        for status in ("pending", "running", "waiting_gate"):
            for run in await self._runs.list(status=status):
                loaded = await self.get(run.id)
                if loaded is not None:
                    active.append(loaded)
        return active

    async def count_runs_for_workflow(self, workflow_id: str) -> int:
        return len(await self._runs.list(workflow_id=workflow_id))

    async def list_for_workflow(self, workflow_id: str) -> Sequence[Run]:
        """The canonical Run rows for one workflow — what a reader renders.

        The durable ``Run`` (with its ``PathEntry`` trace), not the runner's
        companion bookkeeping row: a dashboard shows what happened, and the
        companion row carries idempotency state nobody outside the runner
        should read.

        Newest-first by start time — the store returns rows in an opaque
        insertion order a reader cannot follow, so the one seam every surface
        reads through sorts them. ``created_at`` is an ISO-8601 UTC string, so a
        descending string sort is chronological; a run without one sorts last.
        """
        runs = await self._runs.list(workflow_id=workflow_id)
        return sorted(runs, key=lambda run: run.created_at or "", reverse=True)

    async def record(self, run_id: str) -> Run | None:
        """One canonical Run row, or None."""
        return await self._runs.get(run_id)

    async def record_advance_failure(
        self, run_id: str, *, actor_did: str, fence: RunnerFence | None = None
    ) -> int:
        """Count failures durably under the current runner fence and progress basis."""
        for _ in range(_CAS_RETRIES):
            run = await self._runs.get(run_id)
            state = await self._backend.mutable_read(_STATE_COLLECTION, run_id)
            if run is None or state is None or run.status in TERMINAL_RUN_STATUSES:
                raise RunStateMissingError(f"active run {run_id} is unavailable")
            basis = f"{run.status}:{state['path_len']}"
            count = (
                int(state["advance_failure_count"]) + 1
                if state["advance_failure_basis"] == basis
                else 1
            )
            revision = int(state["advance_failure_revision"])
            won = await self._backend.update_if(
                _STATE_COLLECTION,
                run_id,
                {
                    "advance_failure_count": count,
                    "advance_failure_basis": basis,
                    "advance_failure_revision": revision + 1,
                },
                where={"advance_failure_revision": revision, "path_len": state["path_len"]},
                actor_did=actor_did,
                sink=self._sink,
                fence=fence,
            )
            if won:
                return count
        raise RuntimeError("workflow advance failure count CAS exhausted")

    async def set_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        actor_did: str,
        expected_status: RunStatus | None = None,
        resolution: str | None = None,
        last_error: str | None = None,
        clear_error: bool = False,
        fence: RunnerFence | None = None,
    ) -> bool:
        current = await self._runs.get(run_id)
        if current is None:
            return False
        expected = expected_status or current.status
        _, outcome = await self._runs.transition(
            run_id,
            status,
            actor_did=actor_did,
            expected_status=expected,
            last_error=_storable_error(last_error),
            clear_last_error=clear_error,
            fence=fence,
        )
        if outcome != "applied":
            return False
        if resolution is not None:
            await self._backend.mutable_merge(
                _STATE_COLLECTION,
                run_id,
                {"resolution": resolution},
                actor_did=actor_did,
                sink=self._sink,
                fence=fence,
            )
        return True

    async def set_node_states(
        self,
        run_id: str,
        updates: Mapping[str, NodeState],
        *,
        actor_did: str,
        expected_revision: int,
        fence: RunnerFence | None = None,
    ) -> tuple[FlowRun | None, str]:
        """Write node states under the Run's revision CAS; ``(run, outcome)``."""
        _, outcome = await self._runs.set_node_states(
            run_id,
            updates,
            actor_did=actor_did,
            expected_revision=expected_revision,
            fence=fence,
        )
        if outcome != "applied":
            return None, outcome
        return await self.get(run_id), outcome

    async def append_path(
        self,
        run_id: str,
        entry: Mapping[str, Any],
        *,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> None:
        """Journal an entry with CAS retries, then mirror real outcomes on the Run."""
        await self._append_state_path(run_id, entry, actor_did=actor_did, fence=fence)
        outcome = _TRACEABLE_OUTCOMES.get(str(entry.get("kind")))
        if outcome is None:
            return
        # A skipped branch has no task row anywhere, so this is the only place
        # the fact it was considered and not taken is ever recorded.
        path_entry = PathEntry(
            node_id=str(entry.get("node_id", "")),
            kind=str(entry.get("node_kind", "agent")),  # type: ignore[arg-type]
            outcome=outcome,  # type: ignore[arg-type]
            router_choice=entry.get("chosen"),
            loop_iteration=entry.get("iteration"),
        )
        for _ in range(_CAS_RETRIES):
            current = await self._runs.get(run_id)
            if current is None:
                return
            if any(_same_path_entry(existing, path_entry) for existing in current.path_taken):
                return
            appended = await self._runs.append_path_entry(
                run_id, path_entry, actor_did=actor_did, fence=fence
            )
            if appended is not None:
                return
            await asyncio.sleep(0)
        raise RuntimeError(f"run {run_id} path append lost its CAS race repeatedly")

    async def _append_state_path(
        self,
        run_id: str,
        entry: Mapping[str, Any],
        *,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> None:
        """Append one companion journal event without a read/merge lost update."""
        candidate = dict(entry)
        for _ in range(_CAS_RETRIES):
            state = await self._backend.mutable_read(_STATE_COLLECTION, run_id)
            if state is None:
                # Not a trace line that can be shrugged off: this row carries the
                # settled/skipped/route bookkeeping, so dropping an entry silently
                # double-counts spend and re-decides branches on the next tick.
                raise RunStateMissingError(
                    f"run {run_id} has no workflow state row; its journal cannot be appended to"
                )
            path = list(state.get("path") or [])
            if any(_same_mapping(existing, candidate) for existing in path):
                return
            path_len = int(state.get("path_len", len(path)))
            if path_len != len(path):
                raise RuntimeError(f"run {run_id} workflow journal length is inconsistent")
            won = await self._backend.update_if(
                _STATE_COLLECTION,
                run_id,
                {"path": [*path, candidate], "path_len": path_len + 1},
                where={"path_len": path_len},
                actor_did=actor_did,
                sink=self._sink,
                fence=fence,
            )
            if won:
                return
            await asyncio.sleep(0)
        raise RuntimeError(f"run {run_id} workflow journal append lost its CAS race repeatedly")

    async def record_spend(
        self,
        run_id: str,
        *,
        tokens: int,
        cost_usd: float,
        actor_did: str,
        settlement_key: str | None = None,
        fence: RunnerFence | None = None,
    ) -> bool:
        """Settle a node's usage once, even when ticks retry concurrently."""
        if settlement_key is None:
            return (
                await self._runs.settle_budget(
                    run_id, tokens=tokens, actor_did=actor_did, fence=fence
                )
            ) is not None
        return await self._runs.settle_budget_once(
            run_id,
            settlement_key=settlement_key,
            tokens=tokens,
            actor_did=actor_did,
            fence=fence,
        )


class WorkflowTaskStore:
    """``WorkflowTaskStoreLike`` over ``arcstore.tasks.TaskStore``."""

    def __init__(self, backend: Any, *, actor_did: str, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._tasks = TaskStore(backend, sink=sink)
        self._actor_did = actor_did
        self._sink = sink

    async def create_batch(
        self, tasks: Sequence[Task], *, actor_did: str, fence: RunnerFence | None = None
    ) -> Sequence[Task]:
        return await self._tasks.create_batch(tasks, actor_did=actor_did, fence=fence)

    async def query_by_flow_run(self, flow_run_id: str) -> Sequence[Task]:
        """Scoped read on the run's own rows — never list-then-filter.

        ``json_extract`` walks the nested key inside SQLite, so the tick's cost
        tracks the run's node count rather than the whole board's task count.
        """
        rows = await self._backend.mutable_query(
            _TASK_COLLECTION, where={"metadata.flow_run_id": flow_run_id}
        )
        return [Task.model_validate(row, context={"allow_external_refs": True}) for row in rows]

    async def get(self, task_id: str) -> Task | None:
        return await self._tasks.get(task_id)

    async def update(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> Task | None:
        return await self._tasks.update(task_id, patch, actor_did=actor_did, fence=fence)

    async def update_if(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        where: dict[str, Any],
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> Task | None:
        """Conditionally update one task, preserving a concurrent gate decision."""
        won = await self._backend.update_if(
            _TASK_COLLECTION,
            task_id,
            patch,
            where=where,
            actor_did=actor_did,
            sink=self._sink,
            fence=fence,
        )
        return await self.get(task_id) if won else None

    async def request_cancel(self, task_id: str, *, actor_did: str) -> Task | None:
        return await self._tasks.request_cancel(task_id, actor_did=actor_did)

    async def reclaim_expired(
        self,
        flow_run_id: str,
        *,
        stale_after_s: float,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> Sequence[Task]:
        """Return this run's dead in-flight attempts to the pool (crash resume).

        A row ``in_progress`` past both its own timeout and ``stale_after_s``
        has no live run behind it: the process that claimed it died. It goes
        through the tasks reliability engine's own transitions — ``requeue``
        while attempts remain, ``dead_letter`` once they are spent — each pinned
        to the attempt read here, so an attempt that started since is never
        touched. ``attempts`` is left as is: the next claim increments it, which
        is what gives that claim a new attempt key.
        """
        now = datetime.now(UTC)
        reclaimed: list[Task] = []
        for row in await self.query_by_flow_run(flow_run_id):
            if row.status != "in_progress" or not _attempt_expired(row, now, stale_after_s):
                continue
            moved = await self._reclaim_one(row, now=now, actor_did=actor_did, fence=fence)
            if moved is not None:
                reclaimed.append(moved)
        return reclaimed

    async def _reclaim_one(
        self, row: Task, *, now: datetime, actor_did: str, fence: RunnerFence | None
    ) -> Task | None:
        reason = "attempt abandoned: its process stopped mid-run (reclaimed on resume)"
        # A node executor reads this to refuse a blind re-run of a tool that
        # cannot dedupe its effect (the first attempt may have half-run).
        stamp = {"reclaimed_at": now.isoformat()}
        if row.attempts >= row.max_attempts:
            return await self._tasks.dead_letter(
                row.id,
                actor_did=actor_did,
                resolution=f"failed after {row.attempts} attempt(s) — last attempt abandoned",
                last_error=reason,
                expected_attempts=row.attempts,
                fence=fence,
                metadata_patch=stamp,
            )
        return await self._tasks.requeue(
            row.id,
            actor_did=actor_did,
            last_error=reason,
            next_attempt_at=now.isoformat(),
            expected_attempts=row.attempts,
            fence=fence,
            metadata_patch=stamp,
        )


class RegistryOwnerResolver:
    """``OwnerResolver`` over the arcteam entity registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def resolve_owner(self, handle: str) -> str | None:
        entity = await self._registry.get(handle.lstrip("@"))
        return None if entity is None else str(entity.did)

    async def declared_roles(self) -> frozenset[str]:
        """Every role an ACTIVE registered member holds."""
        return frozenset(
            role
            for entity in await self._registry.list_entities()
            if _is_active(entity)
            for role in entity.roles
        )

    async def roles_of(self, did: str) -> frozenset[str]:
        """The roles the registry gives ``did`` — exact DID only, never a handle.

        A gate decider is named by an authenticated DID; resolving it through a
        handle or alias would let a chat user who controls a matching handle
        borrow another member's roles. Unknown or inactive → no roles.
        """
        if not did.startswith("did:"):
            return frozenset()
        entity = await self._registry.get(did)
        if entity is None or str(entity.did) != did or not _is_active(entity):
            return frozenset()
        return frozenset(entity.roles)


def _is_active(entity: Any) -> bool:
    return str(getattr(entity.status, "value", entity.status)) == "active"


async def build_team_bindings(
    *, backend: Any, operator_signer: Any, identity: Any
) -> tuple[Any, RunNarrator]:
    """The two team-side dependencies a production runner needs.

    Owner resolution and narration both ride the SAME arcteam backend, registry,
    and audit chain the agents use — a runner resolving handles against a
    different registry than the one agents register in would resolve nothing.

    The runner narrates under its OWN key-bound DID (COMP-011), so subscribing
    agents can verify the envelope instead of quarantining it. The audit chain
    is signed by the deployment operator authority, never an ephemeral key, or
    the ``message.sent`` records would be repudiable.
    """
    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry

    audit = AuditLogger(backend, operator_signer)
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    messenger = MessagingService(backend, registry, audit, signer=identity.message_signer())
    narrator = RunNarrator(
        messenger,
        sender_did=identity.did,
        ensure_channel=_channel_admitter(registry, messenger, identity),
        ensure_registered=_runner_registrar(registry, identity),
    )
    # The raw registry, not a resolver: build_workflow_runner owns the wrapping,
    # and returning a pre-wrapped one double-wraps it.
    return registry, narrator


def _runner_registrar(registry: Any, identity: Any) -> Any:
    """Register the runner as a team entity once; every send needs a known sender."""

    async def register() -> None:
        from arcteam.types import Entity, EntityType

        if await registry.get(identity.did) is None:
            await registry.register(
                Entity(
                    did=identity.did,
                    handle="workflow-runner",
                    id="agent://workflow-runner",
                    name="Workflow Runner",
                    type=EntityType.AGENT,
                    public_key=identity.public_key_hex,
                )
            )

    return register


def _channel_admitter(registry: Any, messenger: Any, identity: Any) -> Any:
    """Register the runner and join a bound channel before narrating to it.

    ``MessagingService.send`` refuses a sender that is not a registered entity
    and a member of the target channel. Without this the narrator would be
    fully wired and every post would still be dropped — the same silent
    failure as not wiring it at all, one layer deeper. Auto-join is correct
    here for the same reason it is for the operator: the runner is a trusted
    deployment component, not a participant asking for access.
    """

    register = _runner_registrar(registry, identity)

    async def admit(channel_name: str) -> None:
        from arcteam.types import Channel

        await register()
        channels = await messenger.list_channels()
        existing = next((c for c in channels if c.name == channel_name), None)
        if existing is None:
            await messenger.create_channel(Channel(name=channel_name, members=[identity.did]))
        elif identity.did not in existing.members:
            await messenger.join_channel(channel_name, identity.did)

    return admit


def _storable_error(reason: str | None) -> str | None:
    """A failure reason the Run row will accept AND still be readable afterwards.

    ``Run.last_error`` is validated as free text on every read, so a reason the
    policy rejects (an injection-looking phrase inside a provider error) would
    not just fail to save: it would make the run unloadable. Redact first, then
    withhold rather than store anything the policy would refuse.
    """
    if reason is None:
        return None
    cleaned = sanitize_error_text(reason, limit=500)
    try:
        _validate_free_text(cleaned)
    except ValueError:
        return "error detail withheld: rejected by the stored-text policy"
    return cleaned


def _attempt_expired(row: Task, now: datetime, stale_after_s: float) -> bool:
    """Whether an in-flight attempt is past every bound a live run could still be inside.

    An unreadable or missing start time is expired: an attempt that cannot be
    bounded must not pin its node forever.
    """
    try:
        started = datetime.fromisoformat(row.started_at or "")
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    allowance = reclaim_allowance_s(row.timeout_seconds, stale_after_s)
    return (now - started).total_seconds() >= allowance


def _same_mapping(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare JSON-shaped entries independently of dictionary key order."""
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def _same_path_entry(left: PathEntry, right: PathEntry) -> bool:
    """Compare canonical path entries without considering recording timestamps."""
    return (
        left.node_id == right.node_id
        and left.kind == right.kind
        and left.outcome == right.outcome
        and left.router_choice == right.router_choice
        and left.loop_iteration == right.loop_iteration
    )


__all__ = [
    "FlowRun",
    "RegistryOwnerResolver",
    "RunStateMissingError",
    "WorkflowRunStore",
    "WorkflowTaskStore",
    "build_team_bindings",
]
