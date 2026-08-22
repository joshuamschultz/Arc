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
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arcstore.runs import PathEntry, Run, RunStore
from arcstore.tasks import Task, TaskStore
from arctrust.audit import AuditSink

from .narrator import RunNarrator
from .runner_contracts import RunStatus

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
        initiator_did: str,
        channel: str | None,
        input: Mapping[str, Any],  # noqa: A002 — the definition's own vocabulary
        budget_tokens: int | None,
        budget_cost_usd: float | None,
        budget_wall_clock_s: float | None,
    ) -> FlowRun:
        await self._runs.create(
            Run(
                id=run_id,
                workflow_id=workflow_id,
                workflow_version=version,
                content_hash=content_hash,
                status="running",
                initiator_did=initiator_did,
            )
        )
        await self._backend.mutable_write(
            _STATE_COLLECTION,
            run_id,
            {
                "run_id": run_id,
                "channel": channel,
                "input": dict(input),
                "budget_tokens": budget_tokens,
                "budget_cost_usd": budget_cost_usd,
                "budget_wall_clock_s": budget_wall_clock_s,
                "path": [],
                "path_len": 0,
                "resolution": None,
            },
            actor_did=initiator_did,
            sink=self._sink,
        )
        loaded = await self.get(run_id)
        if loaded is None:  # pragma: no cover — the row was just written
            raise RuntimeError(f"run {run_id} vanished immediately after creation")
        return loaded

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
        """
        return await self._runs.list(workflow_id=workflow_id)

    async def record(self, run_id: str) -> Run | None:
        """One canonical Run row, or None."""
        return await self._runs.get(run_id)

    async def set_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        actor_did: str,
        expected_status: RunStatus | None = None,
        resolution: str | None = None,
    ) -> bool:
        current = await self._runs.get(run_id)
        if current is None:
            return False
        expected = expected_status or current.status
        _, outcome = await self._runs.transition(
            run_id, status, actor_did=actor_did, expected_status=expected
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
            )
        return True

    async def append_path(self, run_id: str, entry: Mapping[str, Any], *, actor_did: str) -> None:
        """Journal an entry with CAS retries, then mirror real outcomes on the Run."""
        await self._append_state_path(run_id, entry, actor_did=actor_did)
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
                run_id, path_entry, actor_did=actor_did
            )
            if appended is not None:
                return
            await asyncio.sleep(0)
        raise RuntimeError(f"run {run_id} path append lost its CAS race repeatedly")

    async def _append_state_path(
        self, run_id: str, entry: Mapping[str, Any], *, actor_did: str
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
    ) -> bool:
        """Settle a node's usage once, even when ticks retry concurrently."""
        if settlement_key is None:
            return (
                await self._runs.settle_budget(run_id, tokens=tokens, actor_did=actor_did)
            ) is not None
        return await self._runs.settle_budget_once(
            run_id,
            settlement_key=settlement_key,
            tokens=tokens,
            actor_did=actor_did,
        )


class WorkflowTaskStore:
    """``WorkflowTaskStoreLike`` over ``arcstore.tasks.TaskStore``."""

    def __init__(self, backend: Any, *, actor_did: str, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._tasks = TaskStore(backend, sink=sink)
        self._actor_did = actor_did
        self._sink = sink

    async def create_batch(self, tasks: Sequence[Task], *, actor_did: str) -> Sequence[Task]:
        return await self._tasks.create_batch(tasks, actor_did=actor_did)

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

    async def update(self, task_id: str, patch: dict[str, Any], *, actor_did: str) -> Task | None:
        return await self._tasks.update(task_id, patch, actor_did=actor_did)

    async def update_if(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        where: dict[str, Any],
        actor_did: str,
    ) -> Task | None:
        """Conditionally update one task, preserving a concurrent gate decision."""
        won = await self._backend.update_if(
            _TASK_COLLECTION,
            task_id,
            patch,
            where=where,
            actor_did=actor_did,
            sink=self._sink,
        )
        return await self.get(task_id) if won else None

    async def request_cancel(self, task_id: str, *, actor_did: str) -> Task | None:
        return await self._tasks.request_cancel(task_id, actor_did=actor_did)


class RegistryOwnerResolver:
    """``OwnerResolver`` over the arcteam entity registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def resolve_owner(self, handle: str) -> str | None:
        entity = await self._registry.get(handle.lstrip("@"))
        return None if entity is None else str(entity.did)


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
    )
    # The raw registry, not a resolver: build_workflow_runner owns the wrapping,
    # and returning a pre-wrapped one double-wraps it.
    return registry, narrator


def _channel_admitter(registry: Any, messenger: Any, identity: Any) -> Any:
    """Register the runner and join a bound channel before narrating to it.

    ``MessagingService.send`` refuses a sender that is not a registered entity
    and a member of the target channel. Without this the narrator would be
    fully wired and every post would still be dropped — the same silent
    failure as not wiring it at all, one layer deeper. Auto-join is correct
    here for the same reason it is for the operator: the runner is a trusted
    deployment component, not a participant asking for access.
    """

    async def admit(channel_name: str) -> None:
        from arcteam.types import Channel, Entity, EntityType

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
        channels = await messenger.list_channels()
        existing = next((c for c in channels if c.name == channel_name), None)
        if existing is None:
            await messenger.create_channel(Channel(name=channel_name, members=[identity.did]))
        elif identity.did not in existing.members:
            await messenger.join_channel(channel_name, identity.did)

    return admit


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
