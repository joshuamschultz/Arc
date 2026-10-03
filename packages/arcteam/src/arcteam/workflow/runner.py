"""WorkflowRunner — the deterministic progressor (SPEC-061 COMP-008).

The lineage is BlastForge's ``pipeline.py`` made generic and durable: the same
sequence-a-stage / verify-the-artifact / await-the-gate loop, but driven by a
signed TOML definition instead of hardcoded roles, and by durable task rows
instead of in-process ``await``, so it survives a restart.

Three properties define this class and every method is written to preserve them:

1. **No LLM, no orchestrator agent.** Progression is deterministic code. The
   only model call anywhere near a workflow happens *inside* a node, run by the
   agent that owns it. An ``llm`` router is not an exception: it is a node whose
   output enum is the declared route ids, and the runner merely follows the
   choice it recorded.
2. **The task-row write IS the handoff (D-538).** Materializing a node's row
   with its owner set is how work moves. Narration and wake signals are
   one-way, and a run must reach a terminal state with every outbound message
   dropped.
3. **Nothing is remembered, everything is re-derived.** The frontier is
   recomputed each tick from run-scoped task rows plus the Run's path taken, so
   a runner that died mid-materialization completes the frontier instead of
   duplicating it, and a completed node is replayed, never re-executed.

Tier arrives at CONSTRUCTION and is never resolved per node
(.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md):
arcteam has no tier source of its own, so the host hands it in, and every audit
event the runner emits names the deployment's true posture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote
from uuid import uuid4

import arcstore
from arcstore.runs import NodeState
from arcstore.tasks import Task
from arcstore.workflow_lease import RunnerFence, WorkflowRunnerLease
from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from .errors import UnsignedWorkflowError
from .narrator import RunNarrator, assert_channel_binding, gate_card_text
from .ownership import assert_owner_is_real
from .runner_budget import RunBudget
from .runner_contracts import (
    RETRYABLE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ArgsResolver,
    BundleSpec,
    DefinitionStoreLike,
    GateNodeSpec,
    Initiator,
    NodeSpec,
    OnFailure,
    OperatorNotifier,
    OwnerResolver,
    PredicateEvaluator,
    RoleRoster,
    RouterNodeSpec,
    RunRecord,
    RunStatus,
    RunStoreLike,
    Tier,
    ToolNodeSpec,
    WorkflowSpec,
    WorkflowTaskStoreLike,
)
from .runner_state import NodeInstance, RunState, derive_node_states
from .stores import RunStateMissingError
from .validator import approver_roles, check_gate_roles

logger = logging.getLogger(__name__)


#: Run ids in this namespace are test runs: unsigned drafts an operator is trying
#: out. A live start can never claim it, so the id alone says what a run is.
TEST_RUN_PREFIX = "test-"

#: The most a test run may spend. A draft nobody has read yet gets a small purse.
TEST_RUN_MAX_COST_USD = 0.50

RunMode = Literal["live", "test"]


def is_test_run(run_id: str) -> bool:
    """Whether ``run_id`` names a test run (see :data:`TEST_RUN_PREFIX`)."""
    return run_id.startswith(TEST_RUN_PREFIX)


class WorkflowRunError(RuntimeError):
    """The run cannot proceed at all."""


class WorkflowRunNotFoundError(WorkflowRunError):
    """No Run record exists for that id."""


class UnsignedWorkflowRefusedError(WorkflowRunError):
    """An unsigned definition was asked to run by an initiator or tier that may not."""


class NodeRetryRefusedError(WorkflowRunError):
    """An operator asked to retry a node that cannot be retried, and why."""


class NodeDecisionError(WorkflowRunError):
    """A node's own declaration could not be resolved — fail the run, closed."""

    def __init__(self, node_id: str, detail: str) -> None:
        super().__init__(f"node {node_id}: {detail}")
        self.node_id = node_id
        self.detail = detail


class WorkflowRunnerLeaseUnavailableError(WorkflowRunError):
    """Another process currently owns the fenced ArcFlow runner lease."""


class NodeStateConflict(WorkflowRunError):  # noqa: N818 — named in the P14-B contract
    """The per-node snapshot lost its revision CAS repeatedly. Infrastructure, never fatal."""


# Failures that say nothing about the workflow: the store, the bus, the lease.
# Retried every tick, forever; they never terminate a run (P14-B step 3).
_INFRA_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
    arcstore.MutationFenceRejectedError,
    WorkflowRunnerLeaseUnavailableError,
    NodeStateConflict,
)
# Client libraries whose every error is a transport failure.
_INFRA_MODULES = frozenset({"nats", "asyncpg"})
# Failures that recur identically on every tick: retrying cannot fix them, so
# the run fails with the reason once they have repeated ``threshold`` times.
_DETERMINISTIC_ERRORS: tuple[type[BaseException], ...] = (
    NodeDecisionError,
    RunStateMissingError,
    ValueError,
    KeyError,
)
# A run that cannot advance this many ticks in a row mails the operator once.
STUCK_RUN_MAIL_AFTER = 20
_LEASE_BACKOFF_CAP_S = 60


def _is_infra_error(exc: BaseException) -> bool:
    if isinstance(exc, _INFRA_ERRORS):
        return True
    return type(exc).__module__.split(".", 1)[0] in _INFRA_MODULES


# A node's inherited outputs ride on its task row, so one huge upstream result
# would make every descendant's prompt huge too. Past this size it is a ref.
UPSTREAM_INLINE_LIMIT_BYTES = 32_768


def _ancestors(node: NodeSpec, definition: WorkflowSpec) -> set[str]:
    """Every node reachable backwards through ``needs``, excluding the node itself."""
    seen: set[str] = set()
    pending = list(node.needs)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(definition.node_by_id(current).needs)
    seen.discard(node.id)
    return seen


def _inline_or_ref(value: Any, producer_task_id: str) -> Any:
    """The output itself when small, else a pointer a node can read on demand."""
    size = len(json.dumps(value, default=str).encode("utf-8"))
    if size <= UPSTREAM_INLINE_LIMIT_BYTES or not producer_task_id:
        return value
    return {
        "artifact_ref": {
            "task_id": producer_task_id,
            "size_bytes": size,
            "read_with": f"list_tasks(task_id={producer_task_id!r}, fields=['output'])",
        }
    }


def _failure_reason(failed: NodeInstance) -> str:
    """Why a run failed: the failing node and the reason the node itself recorded."""
    detail = (failed.task.last_error or "").strip()
    if not detail:
        return f"node failed: {failed.node_id}"
    return f"node {failed.node_id} failed: {detail}"


def _completed_with_failures(failures: list[NodeInstance]) -> str:
    """The resolution of a run that finished although some nodes failed."""
    return "completed with failures: " + "; ".join(_failure_reason(f) for f in failures)


def _descendants(node_id: str, definition: WorkflowSpec) -> set[str]:
    """Every node that reaches ``node_id`` through ``needs``, transitively."""
    found: set[str] = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for node in definition.nodes:
            if current in node.needs and node.id not in found:
                found.add(node.id)
                frontier.append(node.id)
    return found


def _upstream_failed_reason(node_id: str, error: str) -> str:
    """Why a node that never ran did not: the upstream failure, in one line."""
    return f"upstream {node_id} failed: {error}" if error else f"upstream {node_id} failed"


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _assert_safe_name(kind: str, value: str) -> None:
    """A run id and a node id are NAMES. Refuse anything shaped like a path.

    Fail-closed rather than sanitizing: a definition that names a node
    ``../../etc/passwd`` is not a definition to quietly repair, and the runner
    is the last checkpoint before these strings become durable keys and are
    handed to a node adapter that does touch the filesystem.
    """
    if not value or value in (".", "..") or not _SAFE_NAME.match(value):
        raise ValueError(f"unsafe {kind} {value!r}: a {kind} is a name, never a path")


def node_task_id(run_id: str, node_id: str, iteration: int) -> str:
    """The deterministic row id for one node instance — the idempotency anchor.

    Derived, never generated: two runners deciding the same frontier compute the
    same id, so a double-create is impossible even before the store dedupes.

    The separator is one both components are forbidden to contain, because the
    derivation must be UNAMBIGUOUS. Joining on a character the parts may also
    hold lets two different (run, node) pairs collide on one key — and since
    creation is idempotent on that key, a colliding run would adopt another
    run's row and read its output as its own upstream.
    """
    _assert_safe_name("run id", run_id)
    _assert_safe_name("node id", node_id)
    return f"wf/{run_id}/{node_id}/{iteration}"


@dataclass(frozen=True)
class _Decision:
    """What to do with one node this tick."""

    action: Literal["wait", "skip", "go"]
    iteration: int = 0
    reason: str = "condition"


_WAIT = _Decision("wait")


def _gate_decision(task: Task) -> str:
    """What the reviewer chose, read from the row the control plane wrote.

    Falls back to the row's status so a gate resolved through the ordinary
    task-review surface still reads correctly: ``done`` is an approval and
    ``failed`` is a rejection. The recorded decision wins because it carries
    the third outcome — returned for revision — which no status can express.
    """
    recorded = str(task.metadata.get("gate_decision") or "")
    if recorded in ("approved", "rejected", "returned_for_revision"):
        return recorded
    return "approved" if task.status == "done" else "rejected"


class WorkflowRunner:
    """Starts runs, advances the frontier, and rolls terminal state into the Run."""

    def __init__(
        self,
        *,
        tasks: WorkflowTaskStoreLike,
        runs: RunStoreLike,
        definitions: DefinitionStoreLike,
        owners: OwnerResolver,
        runner_did: str,
        tier: Tier,
        evaluate: PredicateEvaluator,
        resolve_args: ArgsResolver,
        narrator: RunNarrator | None = None,
        operator_notifier: OperatorNotifier | None = None,
        roles: RoleRoster | None = None,
        audit_sink: AuditSink | None = None,
        node_max_tokens: int | None = None,
        node_max_cost_usd: float | None = None,
        clock: Callable[[], datetime] | None = None,
        run_workspace_root: Path | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
        tick_failure_threshold: int = 3,
        advance_failure_threshold: int = STUCK_RUN_MAIL_AFTER,
        max_capability_legs: int = 16,
        lease: WorkflowRunnerLease | None = None,
        reclaim_after_s: float = 900.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._definitions = definitions
        self._owners = owners
        self._runner_did = runner_did
        self._tier: Tier = tier
        self._evaluate = evaluate
        self._resolve_args = resolve_args
        self._narrator = narrator
        self._operator_notifier = operator_notifier
        # Absent roster = no role is declared and nobody holds one: a gate that
        # names a role refuses to start, and only the operator decides gates.
        self._roles = roles
        self._sink: AuditSink = audit_sink or NullSink()
        self._node_max_tokens = node_max_tokens
        self._node_max_cost_usd = node_max_cost_usd
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_workspace_root = run_workspace_root
        self._on_close = on_close
        # Whole-tick failure escalation (REQ mirrors the scheduler's
        # consecutive_failures + circuit_breaker_threshold, default 3). See
        # ``run_forever`` for why this counts and escalates but never stops.
        self._tick_failure_threshold = tick_failure_threshold
        if advance_failure_threshold < 1:
            raise ValueError("advance_failure_threshold must be at least one")
        self._advance_failure_threshold = advance_failure_threshold
        self._lease = lease
        # Ceiling on the run's carried trifecta legs (mirrors CarriedLegs'
        # default). A per-run security collection that grows without a bound is
        # the SPEC-009 lesson; truncation is audited, never silent.
        self._max_capability_legs = max_capability_legs
        self._consecutive_tick_failures = 0
        # How long an in-flight attempt may run before resume treats it as
        # abandoned by a dead process (never less than the row's own timeout).
        self._reclaim_after_s = reclaim_after_s
        self._sleep = sleep or asyncio.sleep
        self._lease_backoff = 0
        # Set when resume() could not run (no lease yet, or a run failed it);
        # the first tick that holds the lease finishes it.
        self._resume_pending = False
        self._last_known_channels: list[str] = []
        self._state_condition = asyncio.Condition()
        self._state_version = 0

    # -- public surface ----------------------------------------------------

    @property
    def definitions(self) -> DefinitionStoreLike:
        """The definition store this runner dispatches from.

        Exposed so another surface in this process (the dashboard's control
        plane) composes onto the SAME store, tier, and run plane the engine
        enforces, instead of constructing a second set that could disagree.
        """
        return self._definitions

    @property
    def tasks(self) -> WorkflowTaskStoreLike:
        """The task plane node rows live on."""
        return self._tasks

    @property
    def runs(self) -> RunStoreLike:
        """The run plane this runner writes to."""
        return self._runs

    @property
    def tier(self) -> Tier:
        """The deployment posture this runner enforces."""
        return self._tier

    async def member_roles(self, did: str) -> frozenset[str]:
        """The team-registry roles ``did`` holds; empty when no roster is wired.

        The one place a gate surface learns a chat decider's roles. It is keyed
        on the authenticated DID, so nothing the decider typed can add a role.
        """
        if self._roles is None:
            return frozenset()
        return await self._roles.roles_of(did)

    async def gate_approvers(self, task: Task) -> tuple[str, ...]:
        """Who may decide gate row ``task``, read from the run's PINNED definition.

        Never from the row: a task row is a mutable record, and the approver list
        is authority, so it comes from the signed bundle the run is bound to. The
        row's id must be the one its claimed (run, node, iteration) derives, so a
        row cannot borrow another gate's approvers.

        Raises:
            WorkflowRunError: the row, run, or definition does not line up.
        """
        run_id = str(task.metadata.get("flow_run_id", ""))
        node_id = str(task.metadata.get("node_id", ""))
        iteration = int(task.metadata.get("iteration", -1))
        if not run_id or not node_id or task.id != node_task_id(run_id, node_id, iteration):
            raise WorkflowRunError(f"task {task.id!r} is not a workflow gate row")
        run = await self._require_run(run_id)
        bundle = self._bundle_for_dispatch(run)
        if bundle.content_hash != run.content_hash:
            raise WorkflowRunError("the definition changed under this run")
        node = bundle.definition.node_by_id(node_id)
        if node.kind != "gate":
            raise WorkflowRunError(f"node {node_id!r} is not a gate")
        return tuple(cast(GateNodeSpec, node).approvers)

    async def _refuse_undeclared_gate_roles(
        self, definition: WorkflowSpec, initiator_did: str
    ) -> None:
        """A gate naming a role no team member holds can never be decided: refuse
        the run up front with the reason, rather than strand it at the gate."""
        if not any(
            node.kind == "gate" and approver_roles(cast(GateNodeSpec, node).approvers)
            for node in definition.nodes
        ):
            return
        declared = frozenset() if self._roles is None else await self._roles.declared_roles()
        issues = check_gate_roles(definition, declared)
        if not issues:
            return
        reason = "; ".join(f"{i.node_id}.{i.field}: {i.error} ({i.observed})" for i in issues)
        self._audit(
            "workflow.run.refused",
            target=definition.id,
            outcome="unknown_gate_role",
            actor_did=initiator_did,
            extra={"reason": reason},
        )
        raise WorkflowRunError(f"workflow {definition.id!r} cannot start: {reason}")

    async def start_run(
        self,
        workflow_id: str,
        *,
        input: Mapping[str, Any],  # noqa: A002 — the definition's own vocabulary
        initiator: Initiator,
        initiator_did: str,
        run_id: str | None = None,
        trigger_digest: str | None = None,
        detached: bool = False,
        mode: RunMode = "live",
    ) -> RunRecord:
        """Create the Run record, then materialize the first frontier.

        ``mode="test"`` is the one way an unsigned draft runs above personal
        tier: the run id is minted in the reserved test namespace, trust is not
        asked, the run's spend is capped, and every node row is flagged so the
        agent executing it stubs state-modifying work. A live start can never
        claim a test id.

        ``detached=True`` only creates the Run row and returns: the lease
        holder's next tick materializes the frontier. Without it, a CLI or
        dashboard start would need the singleton lease the live service owns
        for its whole lifetime — no operator could ever start a run.

        ``initiator`` is stated by the caller, never inferred. An unsigned
        definition runs only as an operator-initiated draft test at personal
        tier; an agent or a schedule can never start one, at any tier.
        """
        if not detached:
            await self._require_lease()
        # Check the id before it reaches the store, which resolves it against a
        # directory. The store refuses a traversal id itself — this is the
        # boundary check that means that backstop is never the thing that fires.
        _assert_safe_name("workflow id", workflow_id)
        test_mode = mode == "test"
        if test_mode:
            run_id = run_id or f"{TEST_RUN_PREFIX}{uuid4().hex[:12]}"
        if test_mode != (run_id is not None and is_test_run(run_id)):
            raise WorkflowRunError(
                f"run ids starting with {TEST_RUN_PREFIX!r} are reserved for test runs"
            )
        if test_mode:
            bundle = self._definitions.load(workflow_id)
            if bundle.status == "archived":
                raise WorkflowRunError(f"workflow {workflow_id!r} is archived")
        else:
            try:
                bundle = self._definitions.load_for_run(workflow_id)
            except UnsignedWorkflowError as exc:
                self._deny_unsigned(workflow_id, initiator, initiator_did)
                raise UnsignedWorkflowRefusedError(str(exc)) from exc
        # Trust is `is_verified`, never `status`: status carries lifecycle, and
        # an archived bundle can be validly signed. Keying the gate off status
        # would refuse a definition that is in fact trusted.
        if not bundle.is_verified and not test_mode:
            self._admit_unsigned(workflow_id, initiator, initiator_did)
        definition = bundle.definition
        assert_owner_is_real(workflow_id, definition.owner, definition.nodes)
        assert_channel_binding(definition.channel)
        await self._refuse_undeclared_gate_roles(definition, initiator_did)
        budget = definition.budget
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        # A caller-supplied run id becomes part of every task key this run
        # writes. Check it before the Run row exists, not after.
        _assert_safe_name("run id", run_id)
        run, created = await self._runs.create_run(
            run_id=run_id,
            workflow_id=definition.id,
            version=definition.version,
            content_hash=bundle.content_hash,
            trigger_digest=trigger_digest,
            initiator_did=initiator_did,
            channel=definition.channel,
            input=dict(input),
            budget_tokens=None if budget is None else budget.tokens,
            budget_cost_usd=TEST_RUN_MAX_COST_USD if test_mode else None,
            budget_wall_clock_s=None if budget is None else budget.wall_clock_s,
            fence=self._mutation_fence(),
        )
        self._open_run_workspace(run_id)
        if not created:
            # The same occurrence fired again (a retry after a lost response, a
            # double fire): the existing run carries on. Nothing is restarted,
            # re-announced or re-audited as a start.
            self._audit(
                "workflow.run.start_replayed",
                target=f"{definition.id}/{run_id}",
                outcome="replayed",
                actor_did=initiator_did,
                extra={"initiator": initiator},
            )
            return run if detached else await self.advance(run_id)
        self._audit(
            "workflow.run.started",
            target=f"{definition.id}/{run_id}",
            outcome="started",
            actor_did=initiator_did,
            extra={
                "version": definition.version,
                "content_hash": bundle.content_hash,
                # Who authorized what ran. Without it the chain records that a
                # run started, but not under whose signature — and "who signed
                # the definition behind run 17" stops being reconstructible.
                "signer_did": bundle.signer_did,
                "initiator": initiator,
                "mode": mode,
            },
        )
        if self._narrator is not None:
            await self._narrator.run_started(
                channel=run.channel,
                run_id=run_id,
                workflow_id=definition.id,
                version=definition.version,
            )
        if detached:
            return run
        return await self.advance(run_id)

    def _admit_unsigned(self, workflow_id: str, initiator: Initiator, initiator_did: str) -> None:
        """Allow an unsigned run only as an operator draft test at personal tier.

        An agent authors drafts, so letting it (or a schedule it set) run one
        would make authoring self-approval. Enterprise and federal never run
        unsigned. Everything else raises after an audited denial.
        """
        if initiator != "operator" or self._tier != "personal":
            self._deny_unsigned(workflow_id, initiator, initiator_did)
            raise UnsignedWorkflowRefusedError(
                f"workflow {workflow_id!r} carries no verified operator signature; "
                f"refused for a {initiator}-initiated run at {self._tier} tier"
            )
        self._audit(
            "workflow.run.unsigned_draft",
            target=workflow_id,
            outcome="warning",
            actor_did=initiator_did,
            extra={
                "initiator": initiator,
                "warning": "unsigned draft run: no operator signature verified",
            },
        )

    def _deny_unsigned(self, workflow_id: str, initiator: Initiator, initiator_did: str) -> None:
        self._audit(
            "workflow.run.started",
            target=workflow_id,
            outcome="denied",
            actor_did=initiator_did,
            extra={"initiator": initiator, "reason": "unsigned_workflow"},
        )

    async def advance(self, run_id: str) -> RunRecord:
        """One deterministic tick: settle, decide, materialize, roll up.

        The per-node snapshot is reconciled on the way in (a crash may have left
        it behind the rows) and on the way out, on every exit path, so after any
        ``advance`` it equals the state the rows and journal imply.
        """
        await self._require_lease()
        run = await self._reconcile_node_states(await self._require_run(run_id))
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        return await self._reconcile_node_states(await self._advance_live(run))

    async def resume(self) -> int:
        """Pick up after a restart: reclaim abandoned attempts and repair snapshots.

        For every active run, an in-flight attempt whose process died goes back
        to the pool (a new claim, so a new attempt key), and the node snapshot
        is reconciled from the rows. Nothing is re-materialized here: rows are
        created idempotently by ``node_task_id`` and the next tick continues
        from the frontier. Without the lease this defers to the first tick that
        holds it; a run that fails to resume is retried on the next tick too.
        Returns how many runs were resumed.
        """
        try:
            await self._require_lease()
        except WorkflowRunnerLeaseUnavailableError:
            self._resume_pending = True
            return 0
        self._resume_pending = False
        resumed = 0
        for active in await self._runs.active_runs():
            try:
                await self._resume_run(active)
            except arcstore.MutationFenceRejectedError:
                raise
            except Exception:  # reason: one run must not block resuming the rest
                logger.exception("resuming workflow run %s failed", active.run_id)
                self._resume_pending = True
                continue
            resumed += 1
        return resumed

    async def _resume_run(self, run: RunRecord) -> None:
        reclaimed = await self._tasks.reclaim_expired(
            run.run_id,
            stale_after_s=self._reclaim_after_s,
            actor_did=self._runner_did,
            fence=self._mutation_fence(),
        )
        for task in reclaimed:
            self._audit(
                "workflow.node.reclaimed",
                target=f"{run.workflow_id}/{task.metadata.get('node_id', '')}",
                outcome="reclaimed" if task.status == "todo" else "dead_lettered",
                extra={"run_id": run.run_id, "task_id": task.id, "attempts": task.attempts},
            )
        await self._reconcile_node_states(await self._require_run(run.run_id))

    async def _advance_live(self, run: RunRecord) -> RunRecord:
        run_id = run.run_id
        # Dispatch re-reads through the integrity + tier gate on every tick, but
        # NOT the archived refusal: archiving a workflow must not break the runs
        # already moving through it.
        bundle = self._bundle_for_dispatch(run)
        if bundle.content_hash != run.content_hash:
            return await self._terminate(run_id, "failed", "definition changed under a live run")
        definition = bundle.definition

        wall_clock = self._wall_clock_failure(run)
        if wall_clock is not None:
            return await self._terminate(run_id, "failed", f"budget exhausted: {wall_clock}")
        run = await self._settle_spend(run)
        dimension = self._spent_dimension(run)
        if dimension is not None:
            return await self._terminate(run_id, "failed", f"budget exhausted: {dimension}")

        last_state: RunState | None = None
        for _ in range(len(definition.node_ids) + 2):
            run, changed, last_state = await self._pass(run, definition)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            if not changed:
                break
        return await self._finalize(run, definition, last_state)

    def _bundle_for_dispatch(self, run: RunRecord) -> BundleSpec:
        """The bundle to dispatch ``run`` from. A test run is trusted by no one, so
        the signature gate is not asked of it; it never reaches a live path."""
        if is_test_run(run.run_id):
            return self._definitions.load(run.workflow_id)
        return self._definitions.load_for_dispatch(run.workflow_id)

    async def run_forever(self, *, interval: float = 5.0) -> None:
        """Tick until cancelled — the host's entry point (COMP-009).

        One tick advances every non-terminal run. A tick that raises is logged
        and the loop continues: one poisoned run must not stop the engine for
        every other run in the fleet (the scheduler's store-poison lesson).

        That per-run isolation lives inside ``tick()`` and stays exactly as
        is. What this loop adds is a check ONE LEVEL UP: ``tick()`` itself can
        still raise (its own call to list active runs is not inside that
        per-run guard), and a tick that NEVER stops raising means the engine
        makes zero progress while looking exactly as busy from the outside as
        a healthy one — a log line every ``interval`` seconds is not a signal
        anyone is watching. Consecutive whole-tick failures are counted and,
        at ``_tick_failure_threshold`` (mirrors the task scheduler module's own
        consecutive-failure circuit breaker, default 3), escalated loudly via
        an audit event and, if a narrator is wired, a channel post.

        Deliberately does NOT stop the loop at the threshold. Stopping would
        trade one silent-failure shape for another: the process stays alive
        (nothing signals its death), yet every run in the fleet — including
        ones that were healthy — now never ticks again until an operator
        notices the log and restarts it by hand. Continuing to retry means a
        transient cause (a flaky store connection) self-heals on its own the
        moment a tick succeeds, while the loud escalation is what makes the
        non-transient case visible instead of merely logged.
        """
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reason: one bad tick must not stop the engine
                logger.exception("workflow runner tick failed")
                await self._on_tick_failure(exc)
            else:
                self._consecutive_tick_failures = 0
            await asyncio.sleep(interval)

    async def tick(self) -> int:
        """Advance every active run once. Returns how many were advanced.

        Losing the lease is not a failure of any run: the tick backs off
        (1, 2, 4 … 60 s) and touches nothing until the lease is held again.
        """
        try:
            try:
                await self._require_lease()
            except WorkflowRunnerLeaseUnavailableError:
                await self._back_off_lease()
                return 0
            self._lease_backoff = 0
            if self._resume_pending:
                await self.resume()
            advanced = 0
            runs = await self._runs.active_runs()
            self._last_known_channels = sorted({r.channel for r in runs if r.channel is not None})
            for run in runs:
                try:
                    await self.advance(run.run_id)
                except WorkflowRunnerLeaseUnavailableError:
                    await self._back_off_lease()
                    return advanced
                except Exception as exc:  # reason: one poisoned run must not stall the rest
                    logger.exception("advancing run %s failed", run.run_id)
                    try:
                        await self._on_advance_failure(run, exc)
                    except arcstore.MutationFenceRejectedError:
                        raise
                    except Exception as tracking_error:
                        logger.exception("tracking failed for workflow run %s", run.run_id)
                        self._audit(
                            "workflow.run.advance_tracking_failed",
                            target=run.run_id,
                            outcome="degraded",
                            extra={"error": type(tracking_error).__name__},
                        )
                advanced += 1
            return advanced
        finally:
            async with self._state_condition:
                self._state_version += 1
                self._state_condition.notify_all()

    async def _back_off_lease(self) -> None:
        """Wait out another owner's lease, doubling up to the cap. Audited, never fatal."""
        delay = min(2**self._lease_backoff, _LEASE_BACKOFF_CAP_S)
        self._lease_backoff = min(self._lease_backoff + 1, 6)
        self._audit(
            "workflow.runner.lease_unavailable",
            target=f"runner/{self._runner_did}",
            outcome="backing_off",
            extra={"delay_s": delay},
        )
        await self._sleep(delay)

    async def wait_for_terminal(self, run_id: str) -> RunRecord:
        """Wait for the service-owned tick loop to settle one run.

        This observes the runner's own tick notifications; it never drives the
        frontier itself. A CLI caller therefore cannot accidentally turn a
        one-shot process into a competing runner.
        """
        while True:
            run = await self._require_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            async with self._state_condition:
                observed = self._state_version
                run = await self._require_run(run_id)
                if run.status in TERMINAL_RUN_STATUSES:
                    return run
                if self._state_version != observed:
                    continue
                await self._state_condition.wait()

    async def _on_tick_failure(self, exc: Exception) -> None:
        """Count a whole-tick failure; escalate at the threshold and every
        multiple after, so a long outage re-pages rather than going quiet
        again after the first alert.
        """
        self._consecutive_tick_failures += 1
        count = self._consecutive_tick_failures
        if count < self._tick_failure_threshold or count % self._tick_failure_threshold != 0:
            return
        last_error = str(exc)
        self._audit(
            "workflow.runner.degraded",
            target=f"runner/{self._runner_did}",
            outcome="degraded",
            extra={"consecutive_failures": count, "last_error": last_error},
        )
        if self._narrator is None:
            return
        for channel in self._last_known_channels:
            await self._narrator.runner_degraded(
                channel=channel, consecutive_failures=count, last_error=last_error
            )

    async def _on_advance_failure(self, run: RunRecord, exc: Exception) -> None:
        """Bound a poisoned run's retries without degrading healthy neighbours.

        Three classes, decided by the exception and nothing else:

        * infrastructure (``_INFRA_ERRORS``, any ``nats``/``asyncpg`` error) —
          retried every tick, never terminates the run;
        * deterministic (``_DETERMINISTIC_ERRORS``) — fails identically each
          time, so the run fails with the reason at the threshold;
        * anything else — uncertain, so retried like infrastructure.

        A run still not advancing after ``STUCK_RUN_MAIL_AFTER`` consecutive
        failures mails the operator exactly once; the durable counter only
        passes that number again after the run has made progress.
        """
        count = await self._runs.record_advance_failure(
            run.run_id, actor_did=self._runner_did, fence=self._mutation_fence()
        )
        error = str(exc)
        terminalizing = (
            count >= self._advance_failure_threshold
            and not _is_infra_error(exc)
            and isinstance(exc, _DETERMINISTIC_ERRORS)
        )
        self._audit(
            "workflow.run.advance_failed",
            target=run.run_id,
            outcome="terminalized" if terminalizing else "retrying",
            extra={
                "consecutive_failures": count,
                "last_error": error,
                "error_class": type(exc).__name__,
                "infrastructure": _is_infra_error(exc),
            },
        )
        if not terminalizing:
            if count == STUCK_RUN_MAIL_AFTER:
                await self._notify_operator_of_stuck_run(run, exc, count)
            return
        try:
            await self._terminate(
                run.run_id,
                "failed",
                f"runner advance failed {count} consecutive times: {error}",
            )
        except Exception:
            # The failure count and escalation audit are already durable when the
            # store is healthy. A broken terminalization path remains noisy.
            logger.exception("failed to terminalize poisoned workflow run %s", run.run_id)

    async def _notify_operator_of_stuck_run(
        self, run: RunRecord, exc: BaseException, count: int
    ) -> None:
        """One mail: this run has not advanced for ``count`` ticks, and why."""
        delivered = await self._tell_operator(
            (
                f"Workflow {run.workflow_id} run {run.run_id} has not advanced for {count} "
                f"consecutive ticks ({type(exc).__name__}). The run is still live and "
                "retrying every tick; no further notice until it advances."
            ),
            f"workflow-run:{run.run_id}:stuck:{len(run.path_taken)}",
            run,
        )
        self._audit(
            "workflow.run.operator_notified",
            target=f"{run.workflow_id}/{run.run_id}",
            outcome="delivered" if delivered else "undelivered",
            extra={
                "run_id": run.run_id,
                "reason": "cannot advance",
                "error_class": type(exc).__name__,
                "consecutive_failures": count,
            },
        )

    async def _require_lease(self) -> None:
        """Renew the cross-process fence before advancing any workflow state."""
        if self._lease is None:
            return
        if await self._lease.acquire_or_renew() is None:
            raise WorkflowRunnerLeaseUnavailableError(
                "another process owns the ArcFlow workflow runner lease"
            )

    async def aclose(self) -> None:
        """Release what this runner owns. Idempotent — shutdown may retry."""
        try:
            if self._on_close is not None:
                closer, self._on_close = self._on_close, None
                await closer()
        finally:
            if self._lease is not None:
                await self._lease.release()

    async def cancel(
        self, run_id: str, *, actor_did: str, reason: str = "cancelled by operator"
    ) -> RunRecord:
        """Mark the Run cancelled FIRST, then sweep its node rows.

        Order is the whole point (REQ-235): a node completing during the sweep
        cannot extend a frontier that is already closed, because ``advance``
        refuses a cancelled run before it decides anything.
        """
        run = await self._require_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        flipped = await self._runs.set_status(
            run_id,
            "cancelled",
            actor_did=actor_did,
            expected_status=run.status,
            resolution=reason,
        )
        if not flipped:
            return await self._require_run(run_id)

        for task in await self._tasks.query_by_flow_run(run_id):
            if task.status in ("done", "failed"):
                continue
            try:
                await self._tasks.request_cancel(task.id, actor_did=actor_did)
            except Exception:  # reason: one bad row must not strand the others
                logger.warning("cancel sweep failed for task %s", task.id, exc_info=True)

        # Cancellation is an operator control-plane action, not runner progress:
        # it intentionally bypasses the runner lease fence.
        await self._cancel_open_nodes(run, reason, operator_did=actor_did)
        await self._runs.append_path(
            run_id,
            {"kind": "outcome", "status": "cancelled", "detail": reason},
            actor_did=actor_did,
        )
        self._audit(
            "workflow.run.cancelled",
            target=f"{run.workflow_id}/{run_id}",
            outcome="cancelled",
            actor_did=actor_did,
            extra={"reason": reason},
        )
        if self._narrator is not None:
            await self._narrator.run_outcome(
                channel=run.channel, run_id=run_id, status="cancelled", detail=reason
            )
        return await self._require_run(run_id)

    async def retry_node(
        self,
        run_id: str,
        node_id: str,
        *,
        actor_did: str,
        accept_side_effect_repeat: bool = False,
    ) -> RunRecord:
        """Re-run one failed node of a finished run, keeping everything that completed.

        Only a run that ended in failure can retry (a running run is still being
        driven by the runner). The node gets a fresh instance (the next iteration,
        a fresh attempt budget); its row is written BEFORE the run reopens, so a
        tick can never see a reopened run whose failed node still looks fatal.
        Nodes that already finished are untouched, and never run again.
        Like ``cancel``, this is an operator control-plane action and does not
        need the runner lease.

        The new row is stamped ``operator_retry`` so the executing agent knows a
        prior attempt may have half-run. A non-idempotent tool is then refused
        unless the operator said ``accept_side_effect_repeat``, which stamps
        ``operator_retry_ok`` — the one release the executor honours.
        """
        run = await self._require_run(run_id)
        if run.status not in RETRYABLE_RUN_STATUSES:
            raise NodeRetryRefusedError(
                f"run {run_id} is {run.status}: only a failed run can retry a node"
            )
        bundle = self._definitions.load_for_dispatch(run.workflow_id)
        if bundle.content_hash != run.content_hash:
            raise NodeRetryRefusedError("the definition changed under this run; start a new run")
        definition = bundle.definition
        if node_id not in definition.node_ids:
            raise NodeRetryRefusedError(f"node {node_id!r} is not in workflow {definition.id!r}")
        state = RunState(await self._tasks.query_by_flow_run(run_id), run.path_taken)
        latest = state.latest(node_id)
        if latest is None or latest.task.status != "failed":
            raise NodeRetryRefusedError(f"node {node_id!r} did not fail, so it cannot be retried")
        iteration = latest.iteration + 1
        task = await self._build_task(
            run,
            definition,
            definition.node_by_id(node_id),
            iteration,
            state,
            state.scope(run.input),
            self._legs_for(run, state),
        )
        stamped: dict[str, Any] = {**task.metadata, "operator_retry": True}
        if accept_side_effect_repeat:
            stamped["operator_retry_ok"] = True
        task = task.model_copy(update={"metadata": stamped})
        created = await self._tasks.create_batch([task], actor_did=actor_did)
        await self._record_materialization(run, state, created[0])
        reopened = await self._runs.set_status(
            run_id,
            "running",
            actor_did=actor_did,
            expected_status=run.status,
            resolution=f"retrying node {node_id}",
            clear_error=True,
        )
        if not reopened:
            raise NodeRetryRefusedError(f"run {run_id} changed while retrying; try again")
        self._audit(
            "workflow.node.retried",
            target=f"{run.workflow_id}/{node_id}",
            outcome="retried",
            actor_did=actor_did,
            extra={
                "run_id": run_id,
                "iteration": iteration,
                "previous_error": latest.task.last_error,
                "accepted_repeat": accept_side_effect_repeat,
            },
        )
        return await self._require_run(run_id)

    # -- one pass over the graph -------------------------------------------

    async def _pass(
        self, run: RunRecord, definition: WorkflowSpec
    ) -> tuple[RunRecord, bool, RunState]:
        """Decide every node once against a freshly derived state.

        Returns the state it read alongside the run and change flag, so ``_finalize``
        can compare it against its own read and tell an in-tick completion race from a
        real stall.
        """
        rows = await self._tasks.query_by_flow_run(run.run_id)
        state = RunState(rows, run.path_taken)
        scope = state.scope(run.input)
        changed = await self._reconcile_materializations(run, state)
        revisions, gates_changed = await self._resolve_gates(run, definition, state)
        changed |= gates_changed

        routed = await self._follow_llm_routers(run, definition, state)
        if routed is None:
            return await self._require_run(run.run_id), True, state
        repairs, routes_changed = routed
        changed |= routes_changed

        pending: list[tuple[NodeSpec, int]] = [*revisions, *repairs]
        failure_policy = {node.id: node.on_failure for node in definition.nodes}

        try:
            for node in definition.nodes:
                decision = self._decide(node, state, scope, failure_policy)
                if decision.action == "wait":
                    continue
                if decision.action == "skip":
                    await self._skip(run, node.id, decision.iteration, state, decision.reason)
                    changed = True
                elif node.kind == "router" and cast(RouterNodeSpec, node).mode == "rules":
                    await self._choose_rules_route(
                        run, cast(RouterNodeSpec, node), decision.iteration, state, scope
                    )
                    changed = True
                else:
                    pending.append((node, decision.iteration))
        except NodeDecisionError as exc:
            await self._terminate(run.run_id, "failed", str(exc))
            return await self._require_run(run.run_id), True, state

        try:
            changed |= await self._materialize(run, definition, state, scope, pending)
        except NodeDecisionError as exc:
            await self._terminate(run.run_id, "failed", str(exc))
            return await self._require_run(run.run_id), True, state
        return await self._require_run(run.run_id), changed, state

    def _decide(
        self,
        node: NodeSpec,
        state: RunState,
        scope: Mapping[str, Any],
        failure_policy: Mapping[str, OnFailure],
    ) -> _Decision:
        """Whether this node runs, is skipped, or is not yet decidable."""
        candidate = self._candidate_iteration(node, state, failure_policy)
        if candidate is None:
            return _WAIT
        action, iteration, reason = candidate
        if state.highest_iteration(node.id) >= iteration:
            # Already materialized, routed or skipped at this iteration: a
            # skipped need must not re-journal its dependents' skip every pass.
            return _WAIT
        if action == "skip":
            return _Decision("skip", iteration, reason)
        if node.when is not None:
            try:
                satisfied = self._evaluate(node.when, scope)
            except Exception as exc:
                raise NodeDecisionError(node.id, f"predicate {node.when!r} failed: {exc}") from exc
            if not satisfied:
                return _Decision("skip", iteration)
        return _Decision("go", iteration)

    def _candidate_iteration(
        self, node: NodeSpec, state: RunState, failure_policy: Mapping[str, OnFailure]
    ) -> tuple[Literal["go", "skip"], int, str] | None:
        """Resolve ``needs`` into a decision, or ``None`` to wait.

        A need is satisfied when it is done, skipped, or failed under a policy
        that lets the run go on. Every need must settle, and a skipped need
        skips this node too. A failed ``continue`` node counts as done (its
        dependents run and are told); a failed ``skip_dependents`` node skips
        them, with the reason; a failed ``fail_run`` node is never satisfied.
        """
        if not node.needs:
            return ("go", 0, "condition")
        settled: list[int] = []
        skipped: list[tuple[int, str]] = []
        for need in node.needs:
            status, iteration = state.terminal_state(need)
            if status == "done":
                settled.append(iteration)
            elif status == "skipped":
                skipped.append((iteration, state.skip_reasons.get((need, iteration), "")))
            elif status == "failed":
                policy = failure_policy[need]
                if policy == "continue":
                    settled.append(iteration)
                elif policy == "skip_dependents":
                    error = (state.latest_failure_reason(need) or "").strip()
                    skipped.append((iteration, _upstream_failed_reason(need, error)))
        if skipped:
            iteration = max(i for i, _ in skipped)
            reason = next((r for _, r in skipped if r.startswith("upstream ")), "condition")
            return ("skip", iteration, reason)
        if len(settled) == len(node.needs):
            return ("go", max(settled), "condition")
        return None

    # -- effects ------------------------------------------------------------

    async def _materialize(
        self,
        run: RunRecord,
        definition: WorkflowSpec,
        state: RunState,
        scope: Mapping[str, Any],
        pending: list[tuple[NodeSpec, int]],
    ) -> bool:
        """Write the reachable frontier as task rows — the handoff itself."""
        if not pending:
            return False
        budget = await self._budget_for(run, state)
        legs = self._legs_for(run, state)
        rows: list[Task] = []
        for node, iteration in pending:
            grant = await budget.reserve(
                per_node_tokens=self._node_max_tokens, per_node_cost=self._node_max_cost_usd
            )
            if grant is None:
                # No headroom right now: defer, never fail. A settling node
                # releases its reservation and a later tick admits this one.
                continue
            # The row id IS the identity pair, already unambiguous, and the
            # store dedupes on it — there is no second key to get wrong.
            rows.append(
                await self._build_task(run, definition, node, iteration, state, scope, legs)
            )
        if not rows:
            return False
        created = await self._tasks.create_batch(
            rows, actor_did=self._runner_did, fence=self._mutation_fence()
        )
        changed = False
        for task in created:
            changed |= await self._record_materialization(run, state, task)
        return changed

    async def _build_task(
        self,
        run: RunRecord,
        definition: WorkflowSpec,
        node: NodeSpec,
        iteration: int,
        state: RunState,
        scope: Mapping[str, Any],
        legs: list[str],
    ) -> Task:
        """One node instance as a durable row, owned by exactly one agent."""
        try:
            task_id = node_task_id(run.run_id, node.id, iteration)
        except ValueError as exc:
            raise NodeDecisionError(node.id, str(exc)) from exc
        self._assert_contained_artifacts(node)
        owner_did = await self._owner_for(node, definition)
        upstream = self._upstream_for(node, definition, state)
        blocked_by = [
            instance.task.id
            for need in node.needs
            if (instance := state.latest(need)) is not None and instance.task.status == "done"
        ]
        metadata: dict[str, Any] = {
            "workflow": definition.id,
            "workflow_version": definition.version,
            "flow_run_id": run.run_id,
            "node_id": node.id,
            "node_kind": node.kind,
            "iteration": iteration,
            "idempotency_key": task_id,
            "upstream": upstream,
            "strategy": list(node.strategy),
            "output_schema": node.output_schema,
            "artifacts": list(node.artifacts),
            "timeout_s": node.timeout_s,
            "max_attempts": node.max_attempts or 3,
            # Where the node's prompt and schema files actually live. The
            # runner knows; the executing agent would otherwise have to guess,
            # and a guess that lands in its own workspace makes every declared
            # schema unreadable and every node fail.
            "bundle_root": self._bundle_root(definition.id),
            # What the run has already lit (COMP-015). The node's fresh session
            # starts pre-charged with this, so a composition no single session
            # could complete stays unreachable by splitting it across nodes.
            "accumulated_legs": list(legs),
        }
        failed_upstream = {
            ancestor: reason
            for ancestor, reason in state.upstream_failed().items()
            if ancestor in _ancestors(node, definition)
        }
        if failed_upstream:
            metadata["upstream_failed"] = failed_upstream
        if is_test_run(run.run_id):
            # The executing agent reads these two: it alone knows a tool's
            # classification (to stub state-modifying work) and enforces the cap.
            metadata["mode"] = "test"
            metadata["max_cost_usd"] = TEST_RUN_MAX_COST_USD
        revision_notes = state.revisions.get((node.id, iteration))
        if revision_notes:
            metadata["revision_notes"] = revision_notes
        for field in ("prompt", "skill", "script", "gate", "deliver_to"):
            value = getattr(node, field, None)
            if value is not None:
                metadata[field] = value
        if node.kind == "tool":
            tool_node = cast(ToolNodeSpec, node)
            metadata["tool"] = tool_node.tool
            try:
                metadata["args"] = self._resolve_args(tool_node.args, scope)
            except Exception as exc:
                raise NodeDecisionError(node.id, f"unresolvable args: {exc}") from exc
        if node.kind == "router":
            router_node = cast(RouterNodeSpec, node)
            metadata["routes"] = [route.to for route in router_node.routes]
            metadata["router_mode"] = router_node.mode
        return Task(
            id=task_id,
            title=f"{definition.id}: {node.id}",
            status="review" if node.kind == "gate" else ("todo" if owner_did else "backlog"),
            owner_did=owner_did,
            creator_did=self._runner_did,
            blocked_by=blocked_by,
            tags=["workflow", definition.id],
            metadata=metadata,
            requires_review=node.kind == "gate",
            max_attempts=node.max_attempts or 3,
            timeout_seconds=node.timeout_s,
        )

    @staticmethod
    def _upstream_for(node: NodeSpec, definition: WorkflowSpec, state: RunState) -> dict[str, Any]:
        """Every transitive ancestor's output, the set the validator permits a node to read.

        Handing over only direct ``needs`` left a node that depends on a router
        with no data at all, so its model went hunting through the task table.
        An output too large to inline travels as a reference to the task that
        holds it, never as a megabyte of prompt.
        """
        outputs = state.outputs()
        upstream: dict[str, Any] = {}
        for ancestor in sorted(_ancestors(node, definition)):
            if ancestor not in outputs:
                continue
            value = outputs[ancestor]
            producer = state.latest(ancestor)
            upstream[ancestor] = _inline_or_ref(
                value, producer.task.id if producer is not None else ""
            )
        return upstream

    def _bundle_root(self, workflow_id: str) -> str:
        """The directory this run's definition was loaded from, as a string."""
        root = getattr(self._definitions, "root", None)
        return "" if root is None else str(Path(root) / workflow_id)

    def _legs_for(self, run: RunRecord, state: RunState) -> list[str]:
        """The run's carried legs, bounded — the value stamped on every new node.

        Sorted-then-truncated so two runners deciding the same frontier stamp
        the same set, and truncation is audited because dropping a leg silently
        would weaken the composition check exactly when the run is most charged.
        """
        legs = sorted(state.accumulated_legs())
        if len(legs) <= self._max_capability_legs:
            return legs
        kept = legs[: self._max_capability_legs]
        self._audit(
            "workflow.legs.truncated",
            target=f"{run.workflow_id}/{run.run_id}",
            outcome="truncated",
            extra={
                "run_id": run.run_id,
                "max_legs": self._max_capability_legs,
                "kept": kept,
                "dropped": legs[self._max_capability_legs :],
            },
        )
        return kept

    async def _record_materialization(self, run: RunRecord, state: RunState, task: Task) -> bool:
        """Journal a row exactly once, however many times it is re-derived."""
        node_id = str(task.metadata["node_id"])
        iteration = int(task.metadata["iteration"])
        if (node_id, iteration) in state.materialized:
            return False
        state.record_instance(node_id, iteration, task)
        state.materialized.add((node_id, iteration))
        await self._append(
            run.run_id,
            {
                "kind": "materialized",
                "node_id": node_id,
                "iteration": iteration,
                "task_id": task.id,
                "owner": task.owner_did,
            },
        )
        self._audit(
            "workflow.node.materialized",
            target=f"{run.workflow_id}/{node_id}",
            outcome="materialized",
            extra={
                "run_id": run.run_id,
                "iteration": iteration,
                "task_id": task.id,
                # Growth is only bounded if it is also visible: this is where an
                # operator reads how charged the run was when the node started.
                "accumulated_legs": list(task.metadata.get("accumulated_legs") or ()),
            },
        )
        kind = str(task.metadata.get("node_kind", ""))
        if kind == "gate":
            await self._push_gate_card(run, node_id, task.id)
        if self._narrator is not None:
            if kind == "gate":
                await self._narrator.gate_waiting(
                    channel=run.channel,
                    run_id=run.run_id,
                    node_id=node_id,
                    task_id=task.id,
                    workflow_id=run.workflow_id,
                )
            else:
                await self._narrator.node_started(
                    channel=run.channel, run_id=run.run_id, node_id=node_id, owner=task.owner_did
                )
        return True

    async def _reconcile_node_states(
        self, run: RunRecord, *, operator_did: str | None = None
    ) -> RunRecord:
        """Bring ``node_states`` level with the rows and journal; write only the diff.

        The single writer of the snapshot. It never feeds a decision — the next
        step is always decided from the derived ``RunState`` — so a snapshot a
        crash left behind is repaired here, never acted on.
        """
        rows = await self._tasks.query_by_flow_run(run.run_id)
        desired = derive_node_states(rows, run.path_taken)
        diffs = {
            node_id: state
            for node_id, state in desired.items()
            if run.node_states.get(node_id) != state
        }
        if not diffs:
            return run
        return await self._write_node_states(run, diffs, operator_did=operator_did)

    async def _write_node_states(
        self, run: RunRecord, updates: Mapping[str, NodeState], *, operator_did: str | None = None
    ) -> RunRecord:
        """CAS the snapshot on the run's revision; on conflict re-read and retry."""
        for _ in range(3):
            updated, outcome = await self._runs.set_node_states(
                run.run_id,
                updates,
                actor_did=operator_did or self._runner_did,
                expected_revision=run.revision,
                fence=None if operator_did else self._mutation_fence(),
            )
            if outcome == "applied" and updated is not None:
                return updated
            if outcome == "not_found":
                raise WorkflowRunNotFoundError(run.run_id)
            run = await self._require_run(run.run_id)
        raise NodeStateConflict(f"run {run.run_id}: node state revision kept moving")

    async def _reconcile_materializations(self, run: RunRecord, state: RunState) -> bool:
        """Journal rows that exist but were never recorded (a crash mid-write)."""
        changed = False
        known = [instance for group in state.instances.values() for instance in group]
        for instance in known:
            changed |= await self._record_materialization(run, state, instance.task)
        return changed

    async def _skip(
        self, run: RunRecord, node_id: str, iteration: int, state: RunState, reason: str
    ) -> None:
        """Record a non-run terminal state. Skipped nodes get no task row, ever."""
        state.record_skip(node_id, iteration, reason)
        await self._append(
            run.run_id,
            {"kind": "skipped", "node_id": node_id, "iteration": iteration, "reason": reason},
        )
        self._audit(
            "workflow.node.skipped",
            target=f"{run.workflow_id}/{node_id}",
            outcome="skipped",
            extra={"run_id": run.run_id, "iteration": iteration, "reason": reason},
        )

    async def _choose_rules_route(
        self,
        run: RunRecord,
        node: RouterNodeSpec,
        iteration: int,
        state: RunState,
        scope: Mapping[str, Any],
    ) -> None:
        """Pure predicate evaluation — a rules router never becomes a task row."""
        chosen: str | None = None
        for route in node.routes:
            if route.when is None:
                continue
            try:
                if self._evaluate(route.when, scope):
                    chosen = route.to
                    break
            except Exception as exc:
                raise NodeDecisionError(node.id, f"route predicate failed: {exc}") from exc
        if chosen is None:
            chosen = next((route.to for route in node.routes if route.default), None)
        if chosen is None:
            raise NodeDecisionError(node.id, "no route matched and no default is declared")
        await self._record_route(run, node, iteration, chosen, state)

    async def _follow_llm_routers(
        self, run: RunRecord, definition: WorkflowSpec, state: RunState
    ) -> tuple[list[tuple[NodeSpec, int]], bool] | None:
        """Follow a completed llm router's choice; one repair for an invalid one.

        The answer must be a declared route. An undeclared one gets exactly one
        repair attempt (the router runs again, told what it may choose); a second
        invalid answer fails the node. Never a silent fall-through to a branch.
        ``None`` means the pass should restart against fresh state.
        """
        repairs: list[tuple[NodeSpec, int]] = []
        changed = False
        for node in definition.nodes:
            if node.kind != "router":
                continue
            router = cast(RouterNodeSpec, node)
            if router.mode != "llm":
                continue
            instance = state.latest(node.id)
            if instance is None or instance.task.status != "done":
                continue
            key = (node.id, instance.iteration)
            if key in state.routes or key in state.superseded:
                continue
            chosen = str(instance.output.get("route", ""))
            declared = [route.to for route in router.routes]
            if chosen in declared:
                await self._record_route(run, router, instance.iteration, chosen, state)
                changed = True
            elif node.id in state.repaired:
                await self._fail_node(run, node.id, state, f"router output invalid: {chosen!r}")
                return None
            else:
                repairs.append(
                    await self._request_router_repair(run, router, instance, declared, state)
                )
                changed = True
        return repairs, changed

    async def _request_router_repair(
        self,
        run: RunRecord,
        router: RouterNodeSpec,
        instance: NodeInstance,
        declared: list[str],
        state: RunState,
    ) -> tuple[NodeSpec, int]:
        """Journal the repair and put the router back on the frontier with notes."""
        chosen = str(instance.output.get("route", ""))
        notes = (
            f"Your route {chosen!r} is not declared. Choose exactly one of: {', '.join(declared)}."
        )
        await self._append(
            run.run_id,
            {
                "kind": "repair",
                "node_id": router.id,
                "iteration": instance.iteration,
                "notes": notes,
            },
        )
        # Mirror the journal entry now: the next pass would derive the same.
        state.superseded.add((router.id, instance.iteration))
        state.repaired.add(router.id)
        state.revisions[(router.id, instance.iteration + 1)] = notes
        self._audit(
            "workflow.router.repair_requested",
            target=f"{run.workflow_id}/{router.id}",
            outcome="repair",
            extra={"run_id": run.run_id, "answer": chosen, "declared": declared},
        )
        return router, instance.iteration + 1

    async def _record_route(
        self,
        run: RunRecord,
        node: RouterNodeSpec,
        iteration: int,
        chosen: str,
        state: RunState,
    ) -> None:
        untaken = [route.to for route in node.routes if route.to != chosen]
        state.record_route(node.id, iteration, chosen)
        await self._append(
            run.run_id,
            {
                "kind": "route",
                "node_id": node.id,
                "iteration": iteration,
                "chosen": chosen,
                "skipped": untaken,
            },
        )
        self._audit(
            "workflow.route.selected",
            target=f"{run.workflow_id}/{node.id}",
            outcome=chosen,
            extra={"run_id": run.run_id, "iteration": iteration, "mode": node.mode},
        )
        for target in untaken:
            await self._skip(run, target, iteration, state, "branch not taken")

    async def _resolve_gates(
        self, run: RunRecord, definition: WorkflowSpec, state: RunState
    ) -> tuple[list[tuple[NodeSpec, int]], bool]:
        """Record a gate an operator has since decided, once.

        Returns the node instances a ``return_for_revision`` decision puts back
        on the frontier (REQ-247): the reviewer sends the work back to whoever
        produced it, with notes, and the run keeps going — which is a different
        outcome from failing the run, and the only one that needs new work.
        """
        revisions: list[tuple[NodeSpec, int]] = []
        changed = False
        for node in definition.nodes:
            if node.kind != "gate":
                continue
            instance = state.latest(node.id)
            if instance is None or instance.task.status not in ("done", "failed"):
                continue
            if (node.id, instance.iteration) in state.gates:
                continue
            decision = _gate_decision(instance.task)
            state.gates.add((node.id, instance.iteration))
            if decision == "returned_for_revision":
                # Mark it superseded IN THIS PASS: the durable path is only
                # re-read next tick, and a downstream node decided in between
                # would read the settled gate as an approval and release the
                # very work the reviewer sent back.
                state.superseded.add((node.id, instance.iteration))
                revisions.extend(
                    await self._request_revision(run, definition, node, instance, state)
                )
            await self._append(
                run.run_id,
                {
                    "kind": "gate",
                    "node_id": node.id,
                    "iteration": instance.iteration,
                    "decision": decision,
                },
            )
            self._audit(
                "workflow.gate.resolved",
                target=f"{run.workflow_id}/{node.id}",
                outcome=decision,
                extra={"run_id": run.run_id},
            )
            if self._narrator is not None:
                await self._narrator.gate_resolved(
                    channel=run.channel,
                    run_id=run.run_id,
                    node_id=node.id,
                    decision=decision,
                )
            changed = True
        return revisions, changed

    async def _request_revision(
        self,
        run: RunRecord,
        definition: WorkflowSpec,
        node: NodeSpec,
        instance: NodeInstance,
        state: RunState,
    ) -> list[tuple[NodeSpec, int]]:
        """Put each node this gate reviewed back on the frontier, with notes.

        The next iteration is minted as a fresh instance of the node,
        so a revision is an ordinary new node instance: it materializes, the
        gate re-materializes behind it, and the path taken records both. The
        notes travel on the Run's journal rather than in a message, so the
        agent doing the rework reads what the reviewer actually said.
        """
        notes = str(instance.task.metadata.get("gate_notes") or "")
        pending: list[tuple[NodeSpec, int]] = []
        for need in node.needs:
            iteration = state.materialized_count(need)
            state.revisions[(need, iteration)] = notes
            await self._append(
                run.run_id,
                {"kind": "revision", "node_id": need, "iteration": iteration, "notes": notes},
            )
            pending.append((definition.node_by_id(need), iteration))
        self._audit(
            "workflow.gate.revision_requested",
            target=f"{run.workflow_id}/{node.id}",
            outcome="returned_for_revision",
            extra={"run_id": run.run_id, "nodes": list(node.needs), "notes": notes},
        )
        return pending

    async def _fail_node(self, run: RunRecord, node_id: str, state: RunState, reason: str) -> None:
        """Mark a node failed with its reason; the roll-up applies its ``on_failure``.

        A node whose answer was unusable fails even though its task row said
        done, so the row is rewritten, not just left alone.
        """
        instance = state.latest(node_id)
        if instance is not None and instance.task.status != "failed":
            await self._tasks.update(
                instance.task.id,
                {"status": "failed", "last_error": reason},
                actor_did=self._runner_did,
                fence=self._mutation_fence(),
            )
        self._audit(
            "workflow.node.failed",
            target=f"{run.workflow_id}/{node_id}",
            outcome="failed",
            extra={"run_id": run.run_id, "reason": reason},
        )

    # -- roll-up -------------------------------------------------------------

    async def _finalize(
        self, run: RunRecord, definition: WorkflowSpec, prev_state: RunState | None = None
    ) -> RunRecord:
        """Roll node terminal states into the Run, or escalate a stall.

        ``prev_state`` is the last decide pass's read of the tasks. Comparing it to
        this roll-up's own read is what catches a node settling mid-tick (the read
        race) so it is not mistaken for a stall.
        """
        rows = await self._tasks.query_by_flow_run(run.run_id)
        state = RunState(rows, run.path_taken)
        in_flight = state.in_flight()
        if in_flight:
            desired: RunStatus = (
                "waiting_gate" if any(t.status == "review" for t in in_flight) else "running"
            )
            if run.status != desired:
                await self._runs.set_status(
                    run.run_id,
                    desired,
                    actor_did=self._runner_did,
                    expected_status=run.status,
                    fence=self._mutation_fence(),
                )
                return await self._require_run(run.run_id)
            return run
        failures = state.failures()
        policy = {node.id: node.on_failure for node in definition.nodes}
        fatal = [f for f in failures if policy.get(f.node_id, "fail_run") == "fail_run"]
        if fatal:
            await self._cancel_unreached(run, definition, state, fatal[0])
            return await self._terminate(run.run_id, "failed", _failure_reason(fatal[0]))
        # A task that settled between the decide pass and this roll-up moved the
        # frontier AFTER the pass decided against it — not a stall. The decide pass
        # and this roll-up read the store separately, so a predecessor committing
        # 'done' in the ~40ms between the two reads left its successor un-materialized
        # this tick; the next tick materializes it. Comparing the two reads is what
        # separates "a predecessor just finished" (keep running) from "a reachable
        # node was never materialized because the write was dropped" (genuine stall).
        # Without it, that read race turned a healthy run into a permanent failure
        # (run-f031846f710c).
        if prev_state is not None and self._terminal_states_moved(prev_state, state, definition):
            if run.status != "running":
                await self._runs.set_status(
                    run.run_id,
                    "running",
                    actor_did=self._runner_did,
                    expected_status=run.status,
                    fence=self._mutation_fence(),
                )
                return await self._require_run(run.run_id)
            return run
        undecided = [
            node.id for node in definition.nodes if state.terminal_state(node.id)[0] == "absent"
        ]
        if undecided:
            return await self._terminate(
                run.run_id,
                "failed",
                f"stalled: nothing in flight and {undecided[0]} never became reachable",
            )
        if failures:
            return await self._terminate(
                run.run_id, "done_with_failures", _completed_with_failures(failures)
            )
        return await self._terminate(run.run_id, "done", "all nodes complete")

    async def _cancel_open_nodes(
        self, run: RunRecord, reason: str, *, operator_did: str | None = None
    ) -> None:
        """Journal every node the run ended without finishing as ``cancelled``.

        Journaled, not written straight to ``node_states``: the snapshot is
        re-derived from rows and journal on every reconcile, so only a journal
        entry survives it. ``operator_did`` marks an operator action, which
        carries no runner fence.
        """
        fresh = await self._require_run(run.run_id)
        state = RunState(await self._tasks.query_by_flow_run(run.run_id), fresh.path_taken)
        for node_id in self._known_node_ids(run, state):
            status, iteration = state.terminal_state(node_id)
            if status not in ("absent", "in_flight"):
                continue
            entry = {
                "kind": "cancelled",
                "node_id": node_id,
                "iteration": iteration,
                "reason": reason,
            }
            if operator_did is None:
                await self._append(run.run_id, entry)
            else:
                await self._runs.append_path(run.run_id, entry, actor_did=operator_did)
            state.record_cancel(node_id, iteration, reason)
            self._audit(
                "workflow.node.cancelled",
                target=f"{run.workflow_id}/{node_id}",
                outcome="cancelled",
                actor_did=operator_did,
                extra={"run_id": run.run_id, "reason": reason},
            )
        await self._reconcile_node_states(
            await self._require_run(run.run_id), operator_did=operator_did
        )

    def _known_node_ids(self, run: RunRecord, state: RunState) -> list[str]:
        """The run's nodes: its pinned definition's when still served, else those seen.

        A definition that changed under the run is no longer the run's, so its
        node list is not trusted; only nodes the run itself materialized count.
        """
        try:
            bundle = self._bundle_for_dispatch(run)
        except Exception:  # reason: a refusing definition store must not strand the sweep
            bundle = None
        if bundle is not None and bundle.content_hash == run.content_hash:
            return list(bundle.definition.node_ids)
        return list(state.instances)

    async def _cancel_unreached(
        self, run: RunRecord, definition: WorkflowSpec, state: RunState, failed: NodeInstance
    ) -> None:
        """Say why each node that will never run did not, when a failure ends the run.

        A descendant of the failed node names it and its error; a node on an
        independent branch that never started names the run's failure instead.
        Journaled, so the per-node snapshot and the run view show the reason.
        """
        error = (failed.task.last_error or "").strip()
        downstream = _descendants(failed.node_id, definition)
        for node in definition.nodes:
            if state.terminal_state(node.id)[0] != "absent":
                continue
            reason = (
                _upstream_failed_reason(failed.node_id, error)
                if node.id in downstream
                else f"run failed: {_failure_reason(failed)}"
            )
            state.record_cancel(node.id, 0, reason)
            await self._append(
                run.run_id,
                {"kind": "cancelled", "node_id": node.id, "iteration": 0, "reason": reason},
            )
            self._audit(
                "workflow.node.cancelled",
                target=f"{run.workflow_id}/{node.id}",
                outcome="cancelled",
                extra={"run_id": run.run_id, "reason": reason},
            )

    @staticmethod
    def _terminal_states_moved(prev: RunState, curr: RunState, definition: WorkflowSpec) -> bool:
        """True when any node's terminal state differs between the two reads.

        A change means a task settled after the decide pass read the store — the
        signal that distinguishes an in-tick completion race from a real stall.
        """
        return any(
            prev.terminal_state(node.id)[0] != curr.terminal_state(node.id)[0]
            for node in definition.nodes
        )

    async def _terminate(self, run_id: str, status: RunStatus, resolution: str) -> RunRecord:
        run = await self._require_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        flipped = await self._runs.set_status(
            run_id,
            status,
            actor_did=self._runner_did,
            expected_status=run.status,
            resolution=resolution,
            last_error=resolution if status in ("failed", "done_with_failures") else None,
            fence=self._mutation_fence(),
        )
        if not flipped:
            return await self._require_run(run_id)
        await self._cancel_open_nodes(run, resolution)
        await self._append(run_id, {"kind": "outcome", "status": status, "detail": resolution})
        self._audit(
            "workflow.run.finished",
            target=f"{run.workflow_id}/{run_id}",
            outcome=status,
            extra={"resolution": resolution},
        )
        if self._narrator is not None:
            await self._narrator.run_outcome(
                channel=run.channel, run_id=run_id, status=status, detail=resolution
            )
        if status in ("failed", "done_with_failures"):
            await self._notify_operator_of_failure(run, resolution)
        return await self._require_run(run_id)

    async def _push_gate_card(self, run: RunRecord, node_id: str, task_id: str) -> None:
        """Put the approval card in front of the operator, wherever they are.

        A gate waits on a human, and the narration line only reaches a bound
        channel. The card names the row and the three verbs; a platform that has
        buttons renders them, and every verb lands on the same authorized
        ``/gate`` command. Keyed on the row id, so a re-derived materialization
        or a restarted runner never pushes it twice.
        """
        delivered = await self._tell_operator(
            gate_card_text(run.workflow_id, run.run_id, node_id, task_id),
            f"workflow-gate:{task_id}",
            run,
        )
        self._audit(
            "workflow.gate.card_pushed",
            target=f"{run.workflow_id}/{node_id}",
            outcome="delivered" if delivered else "undelivered",
            extra={"run_id": run.run_id, "task_id": task_id},
        )

    async def _tell_operator(self, text: str, idempotency_key: str, run: RunRecord) -> bool:
        """Hand one notice to the host's operator seam. Never raises; reports delivery.

        The key is stable per run and kind, so a retried or restarted runner that
        reaches the same terminal state cannot notify twice. The run is already
        past the point where a delivery failure could change its outcome.
        """
        if self._operator_notifier is None:
            return False
        try:
            link_path = (
                f"/workflows/{quote(run.workflow_id, safe='')}?run={quote(run.run_id, safe='')}"
            )
            channel = await self._operator_notifier(text, idempotency_key, link_path)
        except Exception:  # reason: the run is already terminal; report, do not raise
            logger.warning("operator notice %s not delivered", idempotency_key, exc_info=True)
            return False
        return channel is not None

    async def _notify_operator_of_failure(self, run: RunRecord, reason: str) -> None:
        """Every terminal failure reaches a human, with the reason, whatever the channel.

        Narration goes only to a bound channel and many workflows have none, so a
        nightly run could fail for weeks unseen. This is addressed to the operator
        and its outcome is audited either way: a notice that could not be sent is
        a recorded fact, not silence.
        """
        delivered = await self._tell_operator(
            f"Workflow {run.workflow_id} v{run.version} failed (run {run.run_id}): {reason}",
            f"workflow-run:{run.run_id}:failed:{len(run.path_taken)}",
            run,
        )
        self._audit(
            "workflow.run.operator_notified",
            target=f"{run.workflow_id}/{run.run_id}",
            outcome="delivered" if delivered else "undelivered",
            extra={"run_id": run.run_id, "reason": reason},
        )

    # -- budget --------------------------------------------------------------

    async def _budget_for(self, run: RunRecord, state: RunState) -> RunBudget:
        """Rehydrate the run's accounting, re-reserving for in-flight nodes."""
        budget = RunBudget(
            max_tokens=run.budget_tokens,
            max_cost_usd=run.budget_cost_usd,
            tokens_spent=run.tokens_spent,
            cost_spent=run.cost_spent,
        )
        for _ in state.in_flight():
            await budget.reserve(
                per_node_tokens=self._node_max_tokens, per_node_cost=self._node_max_cost_usd
            )
        return budget

    async def _settle_spend(self, run: RunRecord) -> RunRecord:
        """Accrue each terminal node's spend into the Run exactly once."""
        rows = await self._tasks.query_by_flow_run(run.run_id)
        state = RunState(rows, run.path_taken)
        settled = False
        for group in state.instances.values():
            for instance in group:
                if instance.task.status not in ("done", "failed"):
                    continue
                key = (instance.node_id, instance.iteration)
                if key in state.settled:
                    continue
                tokens = int(instance.task.metadata.get("tokens_used", 0) or 0)
                cost = float(instance.task.metadata.get("cost_usd", 0.0) or 0.0)
                if not tokens and not cost:
                    continue
                applied = await self._runs.record_spend(
                    run.run_id,
                    tokens=tokens,
                    cost_usd=cost,
                    actor_did=self._runner_did,
                    settlement_key=f"{instance.node_id}:{instance.iteration}",
                    fence=self._mutation_fence(),
                )
                if not applied:
                    state.settled.add(key)
                    continue
                await self._append(
                    run.run_id,
                    {
                        "kind": "settled",
                        "node_id": instance.node_id,
                        "iteration": instance.iteration,
                        "tokens": tokens,
                        "cost": cost,
                    },
                )
                state.settled.add(key)
                settled = True
        return await self._require_run(run.run_id) if settled else run

    def _spent_dimension(self, run: RunRecord) -> str | None:
        if run.budget_tokens is not None and run.tokens_spent >= run.budget_tokens:
            return "tokens"
        if run.budget_cost_usd is not None and run.cost_spent >= run.budget_cost_usd:
            return "cost"
        return None

    def _wall_clock_failure(self, run: RunRecord) -> str | None:
        """Why the wall-clock budget must stop this run, or ``None`` to continue.

        Fail CLOSED on an unreadable start time. Returning "not exceeded" there
        would silently disable the only bound on how long a run may burn — a
        resource control that turns itself off on bad input is worse than one
        that refuses, because nothing ever reports it (REQ-236).
        """
        if run.budget_wall_clock_s is None:
            return None
        if run.started_at is None:
            return "wall clock: run has no start time, so it cannot be bounded"
        try:
            started = datetime.fromisoformat(run.started_at)
        except ValueError:
            return f"wall clock: unreadable start time {run.started_at!r}, so it cannot be bounded"
        if (self._clock() - started).total_seconds() > run.budget_wall_clock_s:
            return "wall clock"
        return None

    # -- small helpers -------------------------------------------------------

    def _open_run_workspace(self, run_id: str) -> Path | None:
        """Create the run's shared desk: ``<root>/runs/<run_id>/`` (D-539).

        Work product is files here; typed output carries only what the graph
        consumes. Because every declared artifact is relative to this directory
        and the runner already refuses a path that escapes it, containment is
        structural rather than a rule someone has to remember.
        """
        if self._run_workspace_root is None:
            return None
        _assert_safe_name("run id", run_id)
        workspace = self._run_workspace_root / "runs" / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _assert_contained_artifacts(self, node: NodeSpec) -> None:
        """A declared artifact is workspace-relative or the node does not run.

        The validator refuses these at authoring time, but a bundle can reach
        dispatch without ever having been validated — loaded straight off disk,
        or signed before a rule existed. The runner is the last checkpoint
        before the node adapter resolves these against a real directory
        (ADR-029 containment, ASI04).
        """
        for artifact in node.artifacts:
            parts = artifact.replace("\\", "/").split("/")
            if not artifact or artifact.startswith("/") or ".." in parts:
                raise NodeDecisionError(node.id, f"artifact {artifact!r} escapes the workspace")

    async def _owner_for(self, node: NodeSpec, definition: WorkflowSpec) -> str | None:
        handle = node.agent or definition.owner
        if handle is None:
            if node.kind == "gate":
                return None  # a gate is resolved by a human, not owned by an agent
            raise NodeDecisionError(node.id, "no agent is named and the workflow has no owner")
        owner = await self._owners.resolve_owner(handle)
        if owner is None:
            raise NodeDecisionError(node.id, f"unknown agent {handle!r}")
        return owner

    async def _require_run(self, run_id: str) -> RunRecord:
        run = await self._runs.get(run_id)
        if run is None:
            raise WorkflowRunNotFoundError(run_id)
        return run

    async def _append(self, run_id: str, entry: Mapping[str, Any]) -> None:
        await self._runs.append_path(
            run_id, entry, actor_did=self._runner_did, fence=self._mutation_fence()
        )

    def _mutation_fence(self) -> RunnerFence | None:
        """The token that protected this tick; operator actions pass no token."""
        return None if self._lease is None else self._lease.fence

    def _audit(
        self,
        action: str,
        *,
        target: str,
        outcome: str,
        actor_did: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        emit(
            AuditEvent(
                actor_did=actor_did or self._runner_did,
                action=action,
                target=target,
                outcome=outcome,
                tier=self._tier,
                extra=dict(extra or {}),
            ),
            self._sink,
        )


def build_workflow_runner(
    *,
    tier: str,
    task_store_backend: Any,
    runner_key_path: Path,
    workspace_root: Path | None = None,
    registry: Any = None,
    operator_public_key: bytes | None = None,
    narrator: RunNarrator | None = None,
    operator_notifier: OperatorNotifier | None = None,
    audit_sink: AuditSink | None = None,
    on_close: Callable[[], Awaitable[None]] | None = None,
    tick_failure_threshold: int = 3,
) -> WorkflowRunner:
    """Assemble a production runner (COMP-009's construction seam).

    Called by ``arcgateway.workflow_runner_host`` on the agent side of the fleet
    service. Every dependency is resolved here rather than in the host, so the
    host stays a lifecycle owner and there is exactly one place that knows how a
    real runner is wired.

    Tier is handed in and flows to BOTH the definition store's gate and the
    runner's audit context — the same value, so enforcement and the audit trail
    can never disagree about the deployment's posture.

    Raises:
        RunnerIdentityUnavailableError: no operator key on disk. A runner with
            no identity must not start: every row it wrote would be
            unattributable, which is worse than not running.
    """
    from .identity import RunnerIdentity
    from .predicates import evaluate as evaluate_predicate
    from .resolver import resolve_args as resolve_node_args
    from .store import DefinitionStore
    from .stores import RegistryOwnerResolver, WorkflowRunStore, WorkflowTaskStore

    identity = RunnerIdentity.load(runner_key_path)
    root = workspace_root or runner_key_path.parent.parent
    sink: AuditSink = audit_sink or NullSink()
    # Pin the operator key the runner's own identity was derived from. Without
    # a pin the store has no authority to verify against, and "signed" degrades
    # to "carries some signature" — which is the whole composition the
    # draft-then-operator-sign lifecycle exists to prevent (LLM06/ASI04).
    # The key is already on disk and already loaded; not passing it here was
    # the security parameter absent AT CONSTRUCTION, one layer up from the
    # component that enforces it.
    pinned = operator_public_key or bytes.fromhex(identity.public_key_hex)
    definitions = DefinitionStore(
        root / "workflows",
        tier=tier,
        operator_public_key=pinned,
        audit=_definition_audit_hook(sink, tier),
    )
    # Record the posture this runner will actually enforce. Tier is handed in by
    # the host from ITS config, while every agent's stringency comes from a
    # different setting, and nothing reconciles the two. Whether they may
    # legitimately differ is a deployment decision — but a runner dispatching at
    # a weaker tier than the fleet believes it has must never be discoverable
    # only by reading code (AU-2: records reflect actual posture, not intended).
    emit(
        AuditEvent(
            actor_did=identity.did,
            action="workflow.runner.constructed",
            target=str(root),
            outcome="ok",
            tier=tier,
            extra={
                "tier_source": "host-supplied",
                "signed_definitions_required": tier != "personal",
            },
        ),
        sink,
    )
    logger.info(
        "workflow runner constructed at tier=%s (signed definitions %s)",
        tier,
        "required" if tier != "personal" else "NOT required",
    )
    team = RegistryOwnerResolver(registry) if registry is not None else _no_registry()
    return WorkflowRunner(
        tasks=WorkflowTaskStore(task_store_backend, actor_did=identity.did),
        runs=WorkflowRunStore(task_store_backend),
        definitions=definitions,
        owners=team,
        roles=team,
        runner_did=identity.did,
        tier=cast(Tier, tier),
        evaluate=evaluate_predicate,
        resolve_args=resolve_node_args,
        narrator=narrator,
        operator_notifier=operator_notifier,
        audit_sink=sink,
        run_workspace_root=root / "shared",
        on_close=on_close,
        tick_failure_threshold=tick_failure_threshold,
        # The DID is audit identity, not lease identity: two processes running
        # the same deployment share it, so the lease generates a per-process
        # owner nonce for fencing.
        lease=WorkflowRunnerLease(task_store_backend),
    )


def _no_registry() -> _NoRegistry:
    """Warn loudly: this runner will start and then fail every run it touches."""
    logger.warning(
        "workflow runner built with no entity registry: no node owner can be "
        "resolved, so every run will fail at its first node. Pass registry= to "
        "build_workflow_runner()."
    )
    return _NoRegistry()


def _definition_audit_hook(sink: AuditSink, tier: str) -> Callable[[str, dict[str, Any]], None]:
    """Route the definition store's lifecycle events into the audit chain.

    The store takes this as an optional hook and stays silent without one. Two
    of its events have no equivalent anywhere else: ``workflow.signed``, which
    the control plane cannot emit because signing happens out-of-band with a key
    that never enters this process, and ``workflow.unsigned_run_permitted``,
    which fires on the dispatch path this runner drives every tick. Unwired,
    a personal-tier deployment runs unsigned definitions and no record of that
    fact exists anywhere (DESIGN section 5, invariant 5).
    """

    def hook(event: str, payload: dict[str, Any]) -> None:
        emit(
            AuditEvent(
                actor_did=str(payload.get("actor_did") or "did:arc:system:definition-store"),
                action=event,
                target=str(payload.get("workflow_id") or ""),
                outcome="ok",
                tier=tier,
                extra=dict(payload),
            ),
            sink,
        )

    return hook


class _NoRegistry:
    """Owner and role resolution with no registry wired: refuse, never guess.

    A node whose owner cannot be resolved fails the run closed. Returning some
    default DID would hand another agent's identity a task row. Likewise no
    role is declared and nobody holds one, so only the operator decides gates.
    """

    async def resolve_owner(self, handle: str) -> str | None:
        return None

    async def declared_roles(self) -> frozenset[str]:
        return frozenset()

    async def roles_of(self, did: str) -> frozenset[str]:
        return frozenset()


__all__ = [
    "STUCK_RUN_MAIL_AFTER",
    "NodeDecisionError",
    "NodeStateConflict",
    "UnsignedWorkflowRefusedError",
    "WorkflowRunError",
    "WorkflowRunNotFoundError",
    "WorkflowRunner",
    "build_workflow_runner",
    "node_task_id",
]
