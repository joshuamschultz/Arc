"""Shared fixtures for the ArcFlow tests (SPEC-061).

Two workstreams built this package concurrently — the definition layer and the
runner. Their fixtures are disjoint and both are needed, so this file carries
both halves rather than either replacing the other.
"""

from __future__ import annotations

from typing import Any

import pytest

# The DESIGN.md §4 example graph, already carrying the join="any" fix for the
# router-exclusivity deadlock the design's own example shipped with.
EXAMPLE_DOCUMENT: dict[str, Any] = {
    "workflow": {
        "schema_version": "1.0",
        "id": "customer-onboarding",
        "version": 4,
        "description": "New customer intake through provisioning",
        "owner": "@sales",
        "channel": "channel://onboarding",
        "budget": {"tokens": 400_000, "wall_clock_s": 1800},
    },
    "trigger": {
        "type": "cron",
        "expression": "0 9 * * MON",
        "active_hours": {
            "start": "08:00",
            "end": "18:00",
            "timezone": "America/Chicago",
        },
    },
    "input": {"schema": "schemas/onboarding_input.json"},
    "node": [
        {
            "id": "collect",
            "kind": "agent",
            "agent": "@sales",
            "skill": "customer-intake",
            "strategy": ["react"],
            "prompt": "prompts/collect.md",
            "output_schema": "schemas/customer_record.json",
            "artifacts": ["customer_record.json"],
            "timeout_s": 300,
            "max_attempts": 3,
        },
        {
            "id": "verify",
            "kind": "tool",
            "tool": "crm_lookup",
            "agent": "@sales",
            "needs": ["collect"],
            "args": {"domain": "$nodes.collect.output.company_domain"},
            "output_schema": "schemas/verification.json",
        },
        {
            "id": "risk_router",
            "kind": "router",
            "mode": "rules",
            "needs": ["verify"],
            "routes": [
                {"to": "provision", "when": "$nodes.verify.output.risk == 'low'"},
                {"to": "manual_review", "default": True},
            ],
        },
        {
            "id": "manual_review",
            "kind": "gate",
            "gate": "human:approve_high_risk",
            "needs": ["risk_router"],
        },
        {
            "id": "provision",
            "kind": "script",
            "script": "scripts/provision.py",
            "agent": "@ops",
            "needs": ["risk_router"],
            "output_schema": "schemas/provisioned.json",
        },
        {
            "id": "qa",
            "kind": "agent",
            "agent": "@reviewer",
            "needs": ["provision", "manual_review"],
            "join": "any",
            "output_schema": "schemas/qa_verdict.json",
        },
        {
            "id": "revise",
            "kind": "agent",
            "agent": "@ops",
            "needs": ["qa"],
            "when": "$nodes.qa.output.verdict == 'revise'",
            "loop_back_to": "provision",
            "max_iterations": 3,
        },
    ],
}


@pytest.fixture
def example_document() -> dict[str, Any]:
    """A deep copy of the canonical example so a test may mutate it freely."""
    import copy

    return copy.deepcopy(EXAMPLE_DOCUMENT)


def minimal_document(**workflow_overrides: Any) -> dict[str, Any]:
    """A two-node linear workflow — the smallest thing that validates."""
    workflow: dict[str, Any] = {"id": "tiny", "owner": "@sales"}
    workflow.update(workflow_overrides)
    return {
        "workflow": workflow,
        "node": [
            {"id": "a", "kind": "agent", "agent": "@sales"},
            {"id": "b", "kind": "agent", "agent": "@sales", "needs": ["a"]},
        ],
    }


# --- runner fixtures ---


from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import Task, TaskStore

from arcteam.workflow.runner import node_task_id

# ---------------------------------------------------------------------------
# Definition doubles (COMP-001)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    to: str
    when: str | None = None
    default: bool = False


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    agent: str | None = None
    needs: tuple[str, ...] = ()
    join: str = "all"
    when: str | None = None
    loop_back_to: str | None = None
    max_iterations: int | None = None
    output_schema: str | None = None
    artifacts: tuple[str, ...] = ()
    strategy: tuple[str, ...] = ()
    timeout_s: float | None = None
    max_attempts: int | None = None
    # router
    mode: str = "rules"
    routes: tuple[Route, ...] = ()
    # tool
    tool: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    # gate
    gate: str | None = None


@dataclass(frozen=True)
class Budget:
    tokens: int | None = None
    wall_clock_s: float | None = None


@dataclass(frozen=True)
class Definition:
    id: str
    version: int = 1
    owner: str | None = None
    channel: str | None = None
    budget: Budget | None = None
    nodes: tuple[Node, ...] = ()

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.nodes)

    def node_by_id(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)


@dataclass(frozen=True)
class Bundle:
    """``status`` is lifecycle; ``is_verified`` is trust. They move independently."""

    definition: Definition
    status: str = "signed"
    signer_did: str | None = "did:arc:local:operator/0f0f0f0f"
    content_hash: str = "sha256:test"

    @property
    def is_verified(self) -> bool:
        return self.signer_did is not None


# ---------------------------------------------------------------------------
# Predicate + resolver doubles (COMP-003, COMP-004)
# ---------------------------------------------------------------------------


# The REAL predicate evaluator and resolver (COMP-003/COMP-004), not stand-ins.
# The runner takes both as injected seams, so tests that wire the real ones are
# the only tests whose "interpolation is refused" assertion means anything —
# a permissive double would accept exactly the strings the real one exists to
# reject.
from arcteam.workflow.errors import TextualInterpolationError  # noqa: F401
from arcteam.workflow.predicates import evaluate  # noqa: F401
from arcteam.workflow.resolver import resolve_args  # noqa: F401

# ---------------------------------------------------------------------------
# Durable plane (COMP-006, COMP-007) over a REAL arcstore backend
# ---------------------------------------------------------------------------


@dataclass
class RunRow:
    run_id: str
    workflow_id: str
    version: int
    content_hash: str
    status: str
    initiator_did: str
    channel: str | None
    input: dict[str, Any]
    path_taken: list[dict[str, Any]]
    budget_tokens: int | None
    budget_cost_usd: float | None
    budget_wall_clock_s: float | None
    tokens_spent: int
    cost_spent: float
    started_at: str | None = None
    resolution: str | None = None


class FlowRunStore:
    """Reference implementation of COMP-006 over the mutable plane."""

    _COLLECTION = "runs"

    def __init__(self, backend: SqliteBackend) -> None:
        self._backend = backend

    async def create_run(
        self,
        *,
        run_id: str,
        workflow_id: str,
        version: int,
        content_hash: str,
        initiator_did: str,
        channel: str | None,
        input: Mapping[str, Any],  # noqa: A002
        budget_tokens: int | None,
        budget_cost_usd: float | None,
        budget_wall_clock_s: float | None,
    ) -> RunRow:
        row = RunRow(
            run_id=run_id,
            workflow_id=workflow_id,
            version=version,
            content_hash=content_hash,
            status="running",
            initiator_did=initiator_did,
            channel=channel,
            input=dict(input),
            path_taken=[],
            budget_tokens=budget_tokens,
            budget_cost_usd=budget_cost_usd,
            budget_wall_clock_s=budget_wall_clock_s,
            tokens_spent=0,
            cost_spent=0.0,
            started_at=datetime.now(UTC).isoformat(),
        )
        await self._backend.mutable_write(
            self._COLLECTION, run_id, row.__dict__, actor_did=initiator_did
        )
        return row

    async def active_runs(self) -> list[RunRow]:
        rows = await self._backend.mutable_query(self._COLLECTION, where={})
        runs = []
        for raw in rows:
            raw.pop("updated_at", None)
            run = RunRow(**raw)
            if run.status not in ("done", "failed", "cancelled"):
                runs.append(run)
        return runs

    async def get(self, run_id: str) -> RunRow | None:
        raw = await self._backend.mutable_read(self._COLLECTION, run_id)
        if raw is None:
            return None
        raw.pop("updated_at", None)
        return RunRow(**raw)

    async def count_runs_for_workflow(self, workflow_id: str) -> int:
        rows = await self._backend.mutable_query(
            self._COLLECTION, where={"workflow_id": workflow_id}
        )
        return len(rows)

    async def set_status(
        self,
        run_id: str,
        status: str,
        *,
        actor_did: str,
        expected_status: str | None = None,
        resolution: str | None = None,
    ) -> bool:
        patch: dict[str, Any] = {"status": status}
        if resolution is not None:
            patch["resolution"] = resolution
        if expected_status is None:
            return await self._backend.mutable_merge(
                self._COLLECTION, run_id, patch, actor_did=actor_did
            )
        return await self._backend.update_if(
            self._COLLECTION,
            run_id,
            patch,
            where={"status": expected_status},
            actor_did=actor_did,
        )

    async def append_path(
        self, run_id: str, entry: Mapping[str, Any], *, actor_did: str
    ) -> None:
        current = await self.get(run_id)
        if current is None:
            return
        path = [*current.path_taken, dict(entry)]
        await self._backend.mutable_merge(
            self._COLLECTION, run_id, {"path_taken": path}, actor_did=actor_did
        )

    async def record_spend(
        self, run_id: str, *, tokens: int, cost_usd: float, actor_did: str
    ) -> None:
        current = await self.get(run_id)
        if current is None:
            return
        await self._backend.mutable_merge(
            self._COLLECTION,
            run_id,
            {
                "tokens_spent": current.tokens_spent + tokens,
                "cost_spent": current.cost_spent + cost_usd,
            },
            actor_did=actor_did,
        )


class FlowTaskStore:
    """Reference implementation of COMP-007 over a real ``TaskStore``.

    ``create_batch`` is idempotent on the caller's key and ``query_by_flow_run``
    is a JSON-scoped read (``metadata.flow_run_id``), never a list-then-filter.
    """

    def __init__(self, backend: SqliteBackend, tasks: TaskStore) -> None:
        self._backend = backend
        self._tasks = tasks
        self.created_keys: list[str] = []
        self.requested_keys: list[str] = []
        self.scoped_queries: list[str] = []
        self.cancelled: list[tuple[str, str]] = []
        self.run_status_at_cancel: list[str | None] = []
        self.observe_run_status: Any = None

    async def create_batch(self, tasks: Sequence[Task], *, actor_did: str) -> Sequence[Task]:
        out: list[Task] = []
        self.requested_keys.extend(task.id for task in tasks)
        for task in tasks:
            existing = await self._tasks.get(task.id)
            if existing is not None:
                out.append(existing)
                continue
            self.created_keys.append(task.id)
            out.append(await self._tasks.create(task))
        return out

    async def query_by_flow_run(self, flow_run_id: str) -> Sequence[Task]:
        self.scoped_queries.append(flow_run_id)
        rows = await self._backend.mutable_query(
            "tasks", where={"metadata.flow_run_id": flow_run_id}
        )
        return [Task.model_validate(r, context={"allow_external_refs": True}) for r in rows]

    async def get(self, task_id: str) -> Task | None:
        return await self._tasks.get(task_id)

    async def update(
        self, task_id: str, patch: dict[str, Any], *, actor_did: str
    ) -> Task | None:
        return await self._tasks.update(task_id, patch, actor_did=actor_did)

    async def request_cancel(self, task_id: str, *, actor_did: str) -> Task | None:
        if self.observe_run_status is not None:
            self.run_status_at_cancel.append(await self.observe_run_status())
        self.cancelled.append((task_id, actor_did))
        return await self._tasks.request_cancel(task_id, actor_did=actor_did)


class Registry:
    """Handle -> DID resolution (the arcteam entity registry's one method here)."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        self._mapping = dict(mapping)

    async def resolve_owner(self, handle: str) -> str | None:
        return self._mapping.get(handle.lstrip("@"))


class RecordingSink:
    """An audit sink that keeps every event."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.action for e in self.events]


class DroppingSender:
    """A messenger that drops EVERY send — the D-538 test harness."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message: Any) -> Any:
        self.attempts += 1
        raise RuntimeError("delivery dropped")


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Any:
        self.sent.append(message)
        return message


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RUNNER_DID = "did:arc:local:workflow-runner/abcd1234"
SALES_DID = "did:arc:local:agent/1111aaaa"
OPS_DID = "did:arc:local:agent/2222bbbb"
REVIEWER_DID = "did:arc:local:agent/3333cccc"


@pytest.fixture
async def backend(tmp_path: Any) -> Any:
    store = SqliteBackend(tmp_path / "flow.db")
    await store.start()
    yield store
    await store.stop()


@pytest.fixture
def registry() -> Registry:
    return Registry({"sales": SALES_DID, "ops": OPS_DID, "reviewer": REVIEWER_DID})


@pytest.fixture
async def stores(backend: Any) -> tuple[FlowTaskStore, FlowRunStore, TaskStore]:
    tasks = TaskStore(backend)
    return FlowTaskStore(backend, tasks), FlowRunStore(backend), tasks


async def complete_node(
    tasks: TaskStore,
    task_id: str,
    agent_did: str,
    output: Mapping[str, Any] | None = None,
    *,
    tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Drive one node the way the owning agent's dispatch loop would.

    Claim the row atomically, then finish it with a validated output. Node spend
    lands in ``metadata`` exactly where the node execution adapter puts it.
    """
    await tasks.start_task(task_id, agent_did)
    if tokens or cost_usd:
        current = await tasks.get(task_id)
        assert current is not None
        meta = {**current.metadata, "tokens_used": tokens, "cost_usd": cost_usd}
        await tasks.update(task_id, {"metadata": meta}, actor_did=agent_did)
    await tasks.finish(
        task_id,
        status="done",
        resolution="node complete",
        actor_did=agent_did,
        output=dict(output or {}),
    )


async def fail_node(tasks: TaskStore, task_id: str, agent_did: str, error: str) -> None:
    await tasks.start_task(task_id, agent_did)
    await tasks.finish(
        task_id,
        status="failed",
        resolution="node failed",
        actor_did=agent_did,
        last_error=error,
    )


def path_kinds(run: Any) -> list[str]:
    return [entry["kind"] for entry in run.path_taken]


def task_id(run_id: str, node_id: str, iteration: int = 0) -> str:
    """The row id a node instance gets — via the real derivation, not a copy."""
    return node_task_id(run_id, node_id, iteration)
