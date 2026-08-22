"""``runs`` domain — Run aggregate + RunStore (SPEC-061 ArcFlow COMP-006).

A durable Run record for one execution of a signed ``workflow.toml``
definition, on its own collection (``"runs"``) of the same shared mutable
plane ``tasks.py`` uses — the operational spool's ``SpoolKind`` is closed and
carries no run identity, so a run gets its own directory rather than a new
spool kind (SDD §Data Model). Modeled directly on ``arcstore.tasks``: frozen
pydantic model, free-text sanitization on the one prose field, status-
conditional ``update_if`` transitions so two writers can never both advance
the same run, and an optional audit sink.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, Protocol

from arctrust.audit import AuditSink
from pydantic import BaseModel, ConfigDict, Field, field_validator

from arcstore.mutation_fence import RunnerFence
from arcstore.tasks import _validate_free_text

RunStatus = Literal["pending", "running", "waiting_gate", "done", "failed", "cancelled"]
NodeKind = Literal["agent", "tool", "script", "router", "gate"]
NodeOutcome = Literal["done", "failed", "skipped"]

_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PathEntry(BaseModel):
    """One step recorded on a Run's path taken (REQ-228, SDD §Data Model).

    Frozen and append-only — ``RunStore.append_path_entry`` is the only writer
    and it never rewrites a prior entry. An untaken branch has no row here at
    all (lazy materialization, SDD §Alternatives Considered): the path taken
    is the honest trace of what actually ran.
    """

    model_config = ConfigDict(frozen=True)

    node_id: str
    kind: NodeKind
    outcome: NodeOutcome
    router_choice: str | None = None
    loop_iteration: int | None = None
    recorded_at: str | None = None


class RunBudget(BaseModel):
    """Reserve-then-settle run-level budget counters (REQ-236).

    ``reserved`` is the outstanding hold a node's dispatch places before it
    runs; ``settle_budget`` moves the actual usage from reserved into spent so
    the two never double-count the same unit of work.
    """

    model_config = ConfigDict(frozen=True)

    tokens_reserved: int = 0
    tokens_spent: int = 0
    wall_clock_seconds_reserved: float = 0.0
    wall_clock_seconds_spent: float = 0.0


class Run(BaseModel):
    """Durable record of one workflow execution (REQ-228, SDD COMP-006).

    Frozen — mutation always goes through :class:`RunStore`, which reads the
    durable row and writes a new one; nothing holds a live ``Run`` and edits
    it in place. ``path_len`` mirrors ``len(path_taken)`` and exists only as
    the compare-and-swap discriminant ``append_path_entry`` uses to keep two
    concurrent appenders from losing one entry to the other (RunStore never
    lets a caller set it directly).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    workflow_id: str
    workflow_version: int
    content_hash: str
    status: RunStatus = "pending"
    initiator_did: str
    runner_did: str | None = None
    budget: RunBudget = Field(default_factory=RunBudget)
    path_taken: list[PathEntry] = Field(default_factory=list)
    path_len: int = 0
    settled: list[str] = Field(default_factory=list)
    settled_len: int = 0
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None

    @field_validator("last_error")
    @classmethod
    def _sanitize_last_error(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_free_text(value)
        return value


class MutableRunBackend(Protocol):
    """The mutable-plane primitives :class:`RunStore` needs (see tasks.py)."""

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        fence: RunnerFence | None = None,
    ) -> None: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        absent_where: dict[str, Any] | None = None,
        fence: RunnerFence | None = None,
    ) -> bool: ...

    async def mutable_increment(
        self,
        collection: str,
        key: str,
        deltas: dict[str, int | float],
        *,
        actor_did: str,
        sink: Any | None = None,
        fence: RunnerFence | None = None,
    ) -> bool: ...

    async def update_if_increment(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        deltas: dict[str, int | float],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        fence: RunnerFence | None = None,
    ) -> bool: ...

    async def append_if_absent(
        self,
        collection: str,
        key: str,
        field: str,
        item: dict[str, Any],
        *,
        length_field: str | None = None,
        actor_did: str,
        sink: Any | None = None,
        fence: RunnerFence | None = None,
    ) -> bool: ...


class RunStore:
    """Run directory over the mutable plane's ``"runs"`` collection."""

    _COLLECTION: ClassVar[str] = "runs"

    def __init__(self, backend: MutableRunBackend, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._sink = sink

    def _load(self, row: dict[str, Any]) -> Run:
        return Run.model_validate(row)

    async def create(self, run: Run, *, fence: RunnerFence | None = None) -> Run:
        now = _now()
        run = run.model_copy(update={"created_at": now, "updated_at": now})
        await self._backend.mutable_write(
            self._COLLECTION,
            run.id,
            run.model_dump(mode="json"),
            actor_did=run.initiator_did,
            sink=self._sink,
            fence=fence,
        )
        return run

    async def get(self, run_id: str) -> Run | None:
        raw = await self._backend.mutable_read(self._COLLECTION, run_id)
        return self._load(raw) if raw is not None else None

    async def list(
        self, *, workflow_id: str | None = None, status: str | None = None
    ) -> list[Run]:
        where: dict[str, Any] = {}
        if workflow_id is not None:
            where["workflow_id"] = workflow_id
        if status is not None:
            where["status"] = status
        rows = await self._backend.mutable_query(self._COLLECTION, where=where)
        return [self._load(row) for row in rows]

    async def transition(
        self,
        run_id: str,
        new_status: RunStatus,
        *,
        actor_did: str,
        expected_status: RunStatus,
        fence: RunnerFence | None = None,
    ) -> tuple[Run | None, str]:
        """Advance a run's status, conditional on its current status (REQ-228).

        The write is atomic on ``where={"status": expected_status}`` (the same
        primitive ``TaskStore.edit``/``set_status`` use) so two writers racing
        to advance the same run frontier resolve to exactly one winner — the
        loser's snapshot no longer matches and it gets ``"conflict"`` rather
        than silently clobbering the winner's transition.

        Returns ``(run, "applied")`` on success, else ``(None, reason)`` where
        ``reason`` is ``"not_found"`` or ``"conflict"``.
        """
        now = _now()
        patch: dict[str, Any] = {"status": new_status, "updated_at": now}
        if new_status in _TERMINAL_STATUSES:
            patch["completed_at"] = now
        won = await self._backend.update_if(
            self._COLLECTION,
            run_id,
            patch,
            where={"status": expected_status},
            actor_did=actor_did,
            sink=self._sink,
            fence=fence,
        )
        if not won:
            current = await self.get(run_id)
            return None, ("not_found" if current is None else "conflict")
        return await self.get(run_id), "applied"

    async def append_path_entry(
        self, run_id: str, entry: PathEntry, *, actor_did: str, fence: RunnerFence | None = None
    ) -> Run | None:
        """Append one entry onto ``path_taken`` (REQ-228/REQ-229).

        Conditional on ``path_len`` matching the snapshot just read (the same
        CAS shape ``transition`` uses on ``status``) so two nodes completing at
        nearly the same instant each land their own entry instead of the
        second silently overwriting the array the first just wrote — a plain
        merge-patch would replace the whole array, not append to it. Returns
        ``None`` if the run is gone or another append won the race first (the
        caller re-reads and retries).
        """
        won = await self._backend.append_if_absent(
            self._COLLECTION,
            run_id,
            "path_taken",
            entry.model_dump(mode="json"),
            length_field="path_len",
            actor_did=actor_did,
            sink=self._sink,
            fence=fence,
        )
        if won:
            current = await self.get(run_id)
            if current is not None:
                return current
            return None
        current = await self.get(run_id)
        if current is None:
            return None
        return (
            current if any(_same_path_entry(item, entry) for item in current.path_taken) else None
        )

    async def reserve_budget(
        self,
        run_id: str,
        *,
        tokens: int = 0,
        wall_clock_seconds: float = 0.0,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> Run | None:
        """Reserve budget against the run before a node dispatches (REQ-236)."""
        deltas: dict[str, int | float] = {}
        if tokens:
            deltas["budget.tokens_reserved"] = tokens
        if wall_clock_seconds:
            deltas["budget.wall_clock_seconds_reserved"] = wall_clock_seconds
        if not deltas:
            return await self.get(run_id)
        won = await self._backend.mutable_increment(
            self._COLLECTION, run_id, deltas, actor_did=actor_did, sink=self._sink, fence=fence
        )
        return await self.get(run_id) if won else None

    async def settle_budget(
        self,
        run_id: str,
        *,
        tokens: int = 0,
        wall_clock_seconds: float = 0.0,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> Run | None:
        """Settle actual usage: move it from reserved into spent (REQ-236).

        Never double-counts against the reservation — settling ``n`` tokens
        both credits ``tokens_spent`` and debits ``tokens_reserved`` by ``n``
        in the same atomic step.
        """
        deltas: dict[str, int | float] = {}
        if tokens:
            deltas["budget.tokens_spent"] = tokens
            deltas["budget.tokens_reserved"] = -tokens
        if wall_clock_seconds:
            deltas["budget.wall_clock_seconds_spent"] = wall_clock_seconds
            deltas["budget.wall_clock_seconds_reserved"] = -wall_clock_seconds
        if not deltas:
            return await self.get(run_id)
        won = await self._backend.mutable_increment(
            self._COLLECTION, run_id, deltas, actor_did=actor_did, sink=self._sink, fence=fence
        )
        return await self.get(run_id) if won else None

    async def settle_budget_once(
        self,
        run_id: str,
        *,
        settlement_key: str,
        tokens: int = 0,
        wall_clock_seconds: float = 0.0,
        actor_did: str,
        fence: RunnerFence | None = None,
    ) -> bool:
        """Atomically claim a settlement key and apply its budget deltas."""
        deltas: dict[str, int | float] = {}
        if tokens:
            deltas["budget.tokens_spent"] = tokens
            deltas["budget.tokens_reserved"] = -tokens
        if wall_clock_seconds:
            deltas["budget.wall_clock_seconds_spent"] = wall_clock_seconds
            deltas["budget.wall_clock_seconds_reserved"] = -wall_clock_seconds
        for _ in range(32):
            current = await self.get(run_id)
            if current is None:
                return False
            if settlement_key in current.settled:
                return False
            won = await self._backend.update_if_increment(
                self._COLLECTION,
                run_id,
                {
                    "settled": [*current.settled, settlement_key],
                    "settled_len": current.settled_len + 1,
                },
                deltas,
                where={"settled_len": current.settled_len},
                actor_did=actor_did,
                sink=self._sink,
                fence=fence,
            )
            if won:
                return True
            await asyncio.sleep(0)
        raise RuntimeError(f"run {run_id} settlement claim lost its CAS race repeatedly")


__all__ = [
    "MutableRunBackend",
    "NodeKind",
    "NodeOutcome",
    "PathEntry",
    "Run",
    "RunBudget",
    "RunStatus",
    "RunStore",
]


def _same_path_entry(left: PathEntry, right: PathEntry) -> bool:
    return (
        left.node_id == right.node_id
        and left.kind == right.kind
        and left.outcome == right.outcome
        and left.router_choice == right.router_choice
        and left.loop_iteration == right.loop_iteration
    )
