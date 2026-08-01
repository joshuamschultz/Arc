"""Seams the workflow runner consumes (SPEC-061 COMP-008).

Everything the runner touches that it does not own is declared here as a
structural ``Protocol`` or a callable alias, for two reasons:

1. **Layering.** The runner must not import ``arcagent`` (it sits below it),
   and the definition/persistence halves live in sibling modules and in
   ``arcstore``. A structural contract keeps the dependency direction honest
   without a concrete import.
2. **One contract per seam.** The runner reads a definition, evaluates a
   predicate, resolves an argument, writes task rows, and moves a Run row.
   Those are five contracts, and this module is the whole list.

Nothing here executes anything: no LLM seam, no model, no strategy selection.
The runner is deterministic progression code and this file is the proof of what
it is allowed to touch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

from arcstore.tasks import Task

Tier = Literal["personal", "enterprise", "federal"]
"""Deployment stringency. Handed to the runner at CONSTRUCTION, never resolved
per node — an audit event that names a tier must name the true one
(.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md)."""

RunStatus = Literal["pending", "running", "waiting_gate", "done", "failed", "cancelled"]
NodeKind = Literal["agent", "tool", "script", "router", "gate"]
BundleStatus = Literal["draft", "signed", "archived"]

TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})
#: Task statuses that mean a node is still owed work (in flight, not terminal).
IN_FLIGHT_TASK_STATUSES: frozenset[str] = frozenset({"backlog", "todo", "in_progress", "review"})


# ---------------------------------------------------------------------------
# Definition (COMP-001) — read-only views of the sibling models
# ---------------------------------------------------------------------------


class RouteSpec(Protocol):
    """One declared branch of a router node."""

    @property
    def to(self) -> str: ...
    @property
    def when(self) -> str | None: ...
    @property
    def default(self) -> bool: ...


class BudgetSpec(Protocol):
    """The workflow's run-level ceiling."""

    @property
    def tokens(self) -> int | None: ...
    @property
    def wall_clock_s(self) -> float | None: ...


class NodeSpec(Protocol):
    """The fields every node kind shares (``NodeBase``).

    Kind-specific fields live on the discriminated-union member, so the runner
    narrows on ``kind`` before reading them (see :class:`RouterNodeSpec`).
    """

    @property
    def id(self) -> str: ...
    @property
    def kind(self) -> NodeKind: ...
    @property
    def agent(self) -> str | None: ...
    @property
    def needs(self) -> Sequence[str]: ...
    @property
    def join(self) -> Literal["all", "any"]: ...
    @property
    def when(self) -> str | None: ...
    @property
    def loop_back_to(self) -> str | None: ...
    @property
    def max_iterations(self) -> int | None: ...
    @property
    def output_schema(self) -> str | None: ...
    @property
    def artifacts(self) -> Sequence[str]: ...
    @property
    def strategy(self) -> Sequence[str]: ...
    @property
    def timeout_s(self) -> float | None: ...
    @property
    def max_attempts(self) -> int | None: ...


class RouterNodeSpec(NodeSpec, Protocol):
    """A router node: declared branches only, never an invented destination."""

    @property
    def mode(self) -> Literal["rules", "llm"]: ...
    @property
    def routes(self) -> Sequence[RouteSpec]: ...


class ToolNodeSpec(NodeSpec, Protocol):
    """A single declared tool call with wired arguments."""

    @property
    def tool(self) -> str: ...
    @property
    def args(self) -> Mapping[str, Any]: ...


class WorkflowSpec(Protocol):
    """A parsed, validated workflow definition."""

    @property
    def id(self) -> str: ...
    @property
    def version(self) -> int: ...
    @property
    def owner(self) -> str | None: ...
    @property
    def channel(self) -> str | None: ...
    @property
    def budget(self) -> BudgetSpec | None: ...
    @property
    def nodes(self) -> Sequence[NodeSpec]: ...
    @property
    def node_ids(self) -> Sequence[str]: ...

    def node_by_id(self, node_id: str) -> NodeSpec: ...


class BundleSpec(Protocol):
    """A definition plus the on-disk facts about it.

    These are bundle properties, not definition properties: no parse or
    validation path can confer trust (REQ-223).

    ``status`` is the LIFECYCLE value and is what a surface renders.
    ``is_verified`` is the TRUST value — true only when the pinned operator
    signature verified — and is the only thing a gate may key off. They are
    deliberately separate: an archived bundle reads ``status="archived"`` while
    still being validly signed, so ``status == "signed"`` would refuse a
    definition that is in fact trusted. Read trust here, never off ``status``.

    ``signer_did`` answers a different question — *who* authorized it — and is
    for the audit record, not for the gate.
    """

    @property
    def definition(self) -> WorkflowSpec: ...
    @property
    def status(self) -> BundleStatus: ...
    @property
    def is_verified(self) -> bool: ...
    @property
    def signer_did(self) -> str | None: ...
    @property
    def content_hash(self) -> str: ...


# ---------------------------------------------------------------------------
# Definition store (COMP-005) — synchronous, filesystem-backed
# ---------------------------------------------------------------------------


class DefinitionStoreLike(Protocol):
    """The definition half of the control plane's world.

    Two gated reads, deliberately split, because admission and dispatch are not
    the same question:

    * ``load_for_run`` — ADMISSION. Signature integrity, tier gate, AND the
      archived refusal. Called once, when a run starts.
    * ``load_for_dispatch`` — every node tick. Signature integrity and the tier
      gate only. Archiving refuses NEW runs (REQ-255); it does not reach in and
      break work already in flight, which is a louder decision than archiving.

    ``load`` applies neither gate and is for rendering only — the runner never
    touches it, or the live path would quietly leave the security gate.
    """

    def load(self, workflow_id: str) -> BundleSpec: ...

    def load_for_run(self, workflow_id: str) -> BundleSpec: ...

    def load_for_dispatch(self, workflow_id: str) -> BundleSpec: ...

    def save_draft(
        self,
        definition: Any,
        *,
        actor_did: str,
        expected_version: int | None,
        files: Mapping[str, bytes] | None = None,
    ) -> BundleSpec:
        """Validates, bumps the version, and always writes ``draft``.

        Raises a validation error carrying ``.issues`` rather than persisting an
        invalid graph.
        """
        ...

    def list_ids(self, *, include_archived: bool = False) -> Sequence[str]: ...

    def archive(self, workflow_id: str, *, actor_did: str) -> BundleSpec: ...

    def unarchive(self, workflow_id: str, *, actor_did: str) -> BundleSpec: ...


class ValidationIssueLike(Protocol):
    """One repairable validation failure, addressed to a field of a node."""

    @property
    def node_id(self) -> str | None: ...
    @property
    def field(self) -> str | None: ...
    @property
    def error(self) -> str: ...
    @property
    def observed(self) -> Any: ...
    @property
    def admissible(self) -> Sequence[str]: ...


# ---------------------------------------------------------------------------
# Pure functions injected at construction (COMP-002, COMP-003, COMP-004)
# ---------------------------------------------------------------------------

PredicateEvaluator = Callable[[str, Mapping[str, Any]], bool]
"""``evaluate(expression, scope) -> bool``. Raises on an unparseable expression
or an unresolvable path; the runner treats a raise as fail-closed."""

ArgsResolver = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
"""``resolve_args(args, scope) -> dict``. Binds ``$nodes.x.output.y`` by VALUE
and refuses a string that merely embeds a reference — never interpolates."""

DefinitionParser = Callable[[Mapping[str, Any]], Any]
"""``parse_definition(document) -> WorkflowDefinition``."""

class DefinitionValidator(Protocol):
    """``validate_definition(definition, pending_files=...) -> issues``; empty is valid.

    ``pending_files`` names files that are about to be written but are not on
    disk yet, so a definition can be validated BEFORE anything is committed —
    the ordering that keeps a rejected edit from having already mutated the
    bundle.
    """

    def __call__(
        self, definition: Any, *, pending_files: frozenset[str] = frozenset()
    ) -> Sequence[ValidationIssueLike]: ...


# ---------------------------------------------------------------------------
# Durable state (COMP-006 runs, COMP-007 task batches) — asynchronous
# ---------------------------------------------------------------------------


class RunRecord(Protocol):
    """The durable Run aggregate (REQ-228)."""

    @property
    def run_id(self) -> str: ...
    @property
    def workflow_id(self) -> str: ...
    @property
    def version(self) -> int: ...
    @property
    def content_hash(self) -> str: ...
    @property
    def status(self) -> RunStatus: ...
    @property
    def initiator_did(self) -> str: ...
    @property
    def channel(self) -> str | None: ...
    @property
    def input(self) -> Mapping[str, Any]: ...
    @property
    def path_taken(self) -> Sequence[Mapping[str, Any]]: ...
    @property
    def budget_tokens(self) -> int | None: ...
    @property
    def budget_cost_usd(self) -> float | None: ...
    @property
    def budget_wall_clock_s(self) -> float | None: ...
    @property
    def tokens_spent(self) -> int: ...
    @property
    def cost_spent(self) -> float: ...
    @property
    def started_at(self) -> str | None: ...
    @property
    def resolution(self) -> str | None: ...


class RunStoreLike(Protocol):
    """Durable Run persistence on the shared mutable plane."""

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
    ) -> RunRecord: ...

    async def get(self, run_id: str) -> RunRecord | None: ...

    async def set_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        actor_did: str,
        expected_status: RunStatus | None = None,
        resolution: str | None = None,
    ) -> bool:
        """Conditional transition. ``False`` means another writer won the race."""
        ...

    async def append_path(
        self, run_id: str, entry: Mapping[str, Any], *, actor_did: str
    ) -> None: ...

    async def record_spend(
        self, run_id: str, *, tokens: int, cost_usd: float, actor_did: str
    ) -> None: ...

    async def active_runs(self) -> Sequence[RunRecord]:
        """Every non-terminal run, for the tick to advance."""
        ...


class WorkflowTaskStoreLike(Protocol):
    """The task-row half: batch materialization plus RUN-SCOPED reads.

    ``query_by_flow_run`` exists so the runner never lists every task and
    filters in Python — the tick cost must not grow with the board.
    """

    async def create_batch(self, tasks: Sequence[Task], *, actor_did: str) -> Sequence[Task]:
        """Create rows atomically, returning the EXISTING row for any id present.

        Idempotent on each task's own id, which is why the runner derives that
        id deterministically from the (run, node, iteration) triple: a crashed
        runner re-invoking this gets back the rows it already created.
        """
        ...

    async def query_by_flow_run(self, flow_run_id: str) -> Sequence[Task]: ...

    async def get(self, task_id: str) -> Task | None: ...

    async def update(
        self, task_id: str, patch: dict[str, Any], *, actor_did: str
    ) -> Task | None: ...

    async def request_cancel(self, task_id: str, *, actor_did: str) -> Task | None: ...


class OwnerResolver(Protocol):
    """Maps a definition's ``@handle`` to the DID that owns the task row."""

    async def resolve_owner(self, handle: str) -> str | None: ...


__all__ = [
    "IN_FLIGHT_TASK_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "ArgsResolver",
    "BudgetSpec",
    "BundleSpec",
    "BundleStatus",
    "DefinitionParser",
    "DefinitionStoreLike",
    "DefinitionValidator",
    "NodeKind",
    "NodeSpec",
    "OwnerResolver",
    "PredicateEvaluator",
    "RouteSpec",
    "RouterNodeSpec",
    "RunRecord",
    "RunStatus",
    "RunStoreLike",
    "Tier",
    "ToolNodeSpec",
    "ValidationIssueLike",
    "WorkflowSpec",
    "WorkflowTaskStoreLike",
]
