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
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from arcstore.tasks import Task
from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from .narrator import RunNarrator, assert_channel_binding
from .runner_budget import RunBudget
from .runner_contracts import (
    TERMINAL_RUN_STATUSES,
    ArgsResolver,
    DefinitionStoreLike,
    NodeSpec,
    OwnerResolver,
    PredicateEvaluator,
    RouterNodeSpec,
    RunRecord,
    RunStatus,
    RunStoreLike,
    Tier,
    ToolNodeSpec,
    WorkflowSpec,
    WorkflowTaskStoreLike,
)
from .runner_state import RunState

logger = logging.getLogger(__name__)


class WorkflowRunError(RuntimeError):
    """The run cannot proceed at all."""


class WorkflowRunNotFoundError(WorkflowRunError):
    """No Run record exists for that id."""


class UnsignedWorkflowRefusedError(WorkflowRunError):
    """An unsigned definition was asked to run above personal tier."""


class NodeDecisionError(WorkflowRunError):
    """A node's own declaration could not be resolved — fail the run, closed."""

    def __init__(self, node_id: str, detail: str) -> None:
        super().__init__(f"node {node_id}: {detail}")
        self.node_id = node_id
        self.detail = detail


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


_WAIT = _Decision("wait")


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
        audit_sink: AuditSink | None = None,
        node_max_tokens: int | None = None,
        node_max_cost_usd: float | None = None,
        clock: Callable[[], datetime] | None = None,
        run_workspace_root: Path | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
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
        self._sink: AuditSink = audit_sink or NullSink()
        self._node_max_tokens = node_max_tokens
        self._node_max_cost_usd = node_max_cost_usd
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_workspace_root = run_workspace_root
        self._on_close = on_close

    # -- public surface ----------------------------------------------------

    async def start_run(
        self,
        workflow_id: str,
        *,
        input: Mapping[str, Any],  # noqa: A002 — the definition's own vocabulary
        initiator_did: str,
        run_id: str | None = None,
    ) -> RunRecord:
        """Create the Run record, then materialize the first frontier."""
        # Check the id before it reaches the store, which resolves it against a
        # directory. The store refuses a traversal id itself — this is the
        # boundary check that means that backstop is never the thing that fires.
        _assert_safe_name("workflow id", workflow_id)
        bundle = self._definitions.load_for_run(workflow_id)
        # Trust is `is_verified`, never `status`: status carries lifecycle, and
        # an archived bundle can be validly signed. Keying the gate off status
        # would refuse a definition that is in fact trusted.
        if self._tier != "personal" and not bundle.is_verified:
            raise UnsignedWorkflowRefusedError(
                f"workflow {workflow_id!r} carries no verified operator signature; "
                f"refused at {self._tier} tier"
            )
        definition = bundle.definition
        assert_channel_binding(definition.channel)
        budget = definition.budget
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        # A caller-supplied run id becomes part of every task key this run
        # writes. Check it before the Run row exists, not after.
        _assert_safe_name("run id", run_id)
        run = await self._runs.create_run(
            run_id=run_id,
            workflow_id=definition.id,
            version=definition.version,
            content_hash=bundle.content_hash,
            initiator_did=initiator_did,
            channel=definition.channel,
            input=dict(input),
            budget_tokens=None if budget is None else budget.tokens,
            budget_cost_usd=None,
            budget_wall_clock_s=None if budget is None else budget.wall_clock_s,
        )
        self._open_run_workspace(run_id)
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
            },
        )
        if self._narrator is not None:
            await self._narrator.run_started(
                channel=run.channel,
                run_id=run_id,
                workflow_id=definition.id,
                version=definition.version,
            )
        return await self.advance(run_id)

    async def advance(self, run_id: str) -> RunRecord:
        """One deterministic tick: settle, decide, materialize, roll up."""
        run = await self._require_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        # Dispatch re-reads through the integrity + tier gate on every tick, but
        # NOT the archived refusal: archiving a workflow must not break the runs
        # already moving through it.
        bundle = self._definitions.load_for_dispatch(run.workflow_id)
        if bundle.content_hash != run.content_hash:
            return await self._terminate(
                run_id, "failed", "definition changed under a live run"
            )
        definition = bundle.definition

        wall_clock = self._wall_clock_failure(run)
        if wall_clock is not None:
            return await self._terminate(run_id, "failed", f"budget exhausted: {wall_clock}")
        run = await self._settle_spend(run)
        dimension = self._spent_dimension(run)
        if dimension is not None:
            return await self._terminate(run_id, "failed", f"budget exhausted: {dimension}")

        for _ in range(len(definition.node_ids) + 2):
            run, changed = await self._pass(run, definition)
            if run.status in TERMINAL_RUN_STATUSES:
                return run
            if not changed:
                break
        return await self._finalize(run, definition)

    async def run_forever(self, *, interval: float = 5.0) -> None:
        """Tick until cancelled — the host's entry point (COMP-009).

        One tick advances every non-terminal run. A tick that raises is logged
        and the loop continues: one poisoned run must not stop the engine for
        every other run in the fleet (the scheduler's store-poison lesson).
        """
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: one bad tick must not stop the engine
                logger.exception("workflow runner tick failed")
            await asyncio.sleep(interval)

    async def tick(self) -> int:
        """Advance every active run once. Returns how many were advanced."""
        advanced = 0
        for run in await self._runs.active_runs():
            try:
                await self.advance(run.run_id)
            except Exception:  # reason: one poisoned run must not stall the rest
                logger.exception("advancing run %s failed", run.run_id)
            advanced += 1
        return advanced

    async def aclose(self) -> None:
        """Release what this runner owns. Idempotent — shutdown may retry."""
        if self._on_close is not None:
            closer, self._on_close = self._on_close, None
            await closer()

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

        await self._append(run_id, {"kind": "outcome", "status": "cancelled", "detail": reason})
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

    # -- one pass over the graph -------------------------------------------

    async def _pass(
        self, run: RunRecord, definition: WorkflowSpec
    ) -> tuple[RunRecord, bool]:
        """Decide every node once against a freshly derived state."""
        rows = await self._tasks.query_by_flow_run(run.run_id)
        state = RunState(rows, run.path_taken)
        scope = state.scope(run.input)
        changed = await self._reconcile_materializations(run, state)
        changed |= await self._resolve_gates(run, definition, state)

        routed = await self._follow_llm_routers(run, definition, state)
        if routed is None:
            return await self._require_run(run.run_id), True
        changed |= routed

        looped = await self._follow_loops(run, definition, state)
        if looped is None:
            return await self._require_run(run.run_id), True
        pending, loop_changed = looped
        changed |= loop_changed

        try:
            for node in definition.nodes:
                decision = self._decide(node, state, scope)
                if decision.action == "wait":
                    continue
                if decision.action == "skip":
                    await self._skip(run, node.id, decision.iteration, state, "condition")
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
            return await self._require_run(run.run_id), True

        try:
            changed |= await self._materialize(run, definition, state, scope, pending)
        except NodeDecisionError as exc:
            await self._terminate(run.run_id, "failed", str(exc))
            return await self._require_run(run.run_id), True
        return await self._require_run(run.run_id), changed

    def _decide(
        self, node: NodeSpec, state: RunState, scope: Mapping[str, Any]
    ) -> _Decision:
        """Whether this node runs, is skipped, or is not yet decidable."""
        candidate = self._candidate_iteration(node, state)
        if candidate is None:
            return _WAIT
        action, iteration = candidate
        if action == "skip":
            return _Decision("skip", iteration)
        if state.highest_iteration(node.id) >= iteration:
            return _WAIT
        if node.when is not None:
            try:
                satisfied = self._evaluate(node.when, scope)
            except Exception as exc:
                raise NodeDecisionError(node.id, f"predicate {node.when!r} failed: {exc}") from exc
            if not satisfied:
                return _Decision("skip", iteration)
        return _Decision("go", iteration)

    def _candidate_iteration(
        self, node: NodeSpec, state: RunState
    ) -> tuple[Literal["go", "skip"], int] | None:
        """Resolve ``needs`` + ``join`` into a decision, or ``None`` to wait.

        A need is satisfied when it is done OR skipped; ``join="any"`` fires on
        the first done need, ``join="all"`` requires them all and propagates a
        skip. Exactly one non-run terminal state, and it travels transitively.
        """
        if not node.needs:
            return ("go", 0)
        done: list[int] = []
        skipped: list[int] = []
        for need in node.needs:
            status, iteration = state.terminal_state(need)
            if status == "done":
                done.append(iteration)
            elif status == "skipped":
                skipped.append(iteration)
        if node.join == "any":
            if done:
                return ("go", max(done))
            if len(skipped) == len(node.needs):
                return ("skip", max(skipped))
            return None
        if skipped:
            return ("skip", max(skipped))
        if len(done) == len(node.needs):
            return ("go", max(done))
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
            rows.append(await self._build_task(run, definition, node, iteration, state, scope))
        if not rows:
            return False
        created = await self._tasks.create_batch(rows, actor_did=self._runner_did)
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
    ) -> Task:
        """One node instance as a durable row, owned by exactly one agent."""
        try:
            task_id = node_task_id(run.run_id, node.id, iteration)
        except ValueError as exc:
            raise NodeDecisionError(node.id, str(exc)) from exc
        self._assert_contained_artifacts(node)
        owner_did = await self._owner_for(node, definition)
        outputs = state.outputs()
        upstream = {need: outputs[need] for need in node.needs if need in outputs}
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
        }
        for field in ("prompt", "skill", "script", "gate"):
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
            metadata["routes"] = [route.to for route in cast(RouterNodeSpec, node).routes]
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

    async def _record_materialization(
        self, run: RunRecord, state: RunState, task: Task
    ) -> bool:
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
            extra={"run_id": run.run_id, "iteration": iteration, "task_id": task.id},
        )
        if self._narrator is not None:
            kind = str(task.metadata.get("node_kind", ""))
            if kind == "gate":
                await self._narrator.gate_waiting(
                    channel=run.channel, run_id=run.run_id, node_id=node_id
                )
            else:
                await self._narrator.node_started(
                    channel=run.channel, run_id=run.run_id, node_id=node_id, owner=task.owner_did
                )
        return True

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
        state.record_skip(node_id, iteration)
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
    ) -> bool | None:
        """Follow a completed llm router's choice. ``None`` means the run failed."""
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
            if (node.id, instance.iteration) in state.routes:
                continue
            chosen = str(instance.output.get("route", ""))
            declared = [route.to for route in router.routes]
            if chosen not in declared:
                await self._terminate(
                    run.run_id,
                    "failed",
                    f"node {node.id}: undeclared route {chosen!r}, declared {declared}",
                )
                return None
            await self._record_route(run, router, instance.iteration, chosen, state)
            changed = True
        return changed

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

    async def _follow_loops(
        self, run: RunRecord, definition: WorkflowSpec, state: RunState
    ) -> tuple[list[tuple[NodeSpec, int]], bool] | None:
        """Mint the next iteration of a declared back-edge's target.

        The counter is keyed on the TARGET, not on the node carrying the edge, so
        a router inside the loop body cannot reset it. Exhaustion fails the node.
        """
        pending: list[tuple[NodeSpec, int]] = []
        changed = False
        for node in definition.nodes:
            target_id = node.loop_back_to
            if target_id is None:
                continue
            status, iteration = state.terminal_state(node.id)
            if status != "done" or (node.id, iteration) in state.loops:
                continue
            taken = state.materialized_count(target_id)
            bound = node.max_iterations or 1
            state.record_loop(node.id, iteration)
            if taken >= bound:
                await self._append(
                    run.run_id,
                    {
                        "kind": "loop",
                        "node_id": node.id,
                        "iteration": iteration,
                        "target": target_id,
                        "outcome": "exhausted",
                    },
                )
                await self._fail_node(
                    run, node.id, state, f"max_iterations ({bound}) reached looping to {target_id}"
                )
                return None
            await self._append(
                run.run_id,
                {
                    "kind": "loop",
                    "node_id": node.id,
                    "iteration": iteration,
                    "target": target_id,
                },
            )
            pending.append((definition.node_by_id(target_id), taken))
            changed = True
        return pending, changed

    async def _resolve_gates(
        self, run: RunRecord, definition: WorkflowSpec, state: RunState
    ) -> bool:
        """Record a gate that an operator has since decided, once."""
        changed = False
        for node in definition.nodes:
            if node.kind != "gate":
                continue
            instance = state.latest(node.id)
            if instance is None or instance.task.status not in ("done", "failed"):
                continue
            if (node.id, instance.iteration) in state.gates:
                continue
            decision = "approved" if instance.task.status == "done" else "rejected"
            state.gates.add((node.id, instance.iteration))
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
        return changed

    async def _fail_node(
        self, run: RunRecord, node_id: str, state: RunState, reason: str
    ) -> None:
        instance = state.latest(node_id)
        if instance is not None and instance.task.status not in ("done", "failed"):
            await self._tasks.update(
                instance.task.id,
                {"status": "failed", "last_error": reason},
                actor_did=self._runner_did,
            )
        self._audit(
            "workflow.node.failed",
            target=f"{run.workflow_id}/{node_id}",
            outcome="failed",
            extra={"run_id": run.run_id, "reason": reason},
        )
        await self._terminate(run.run_id, "failed", f"node {node_id}: {reason}")

    # -- roll-up -------------------------------------------------------------

    async def _finalize(self, run: RunRecord, definition: WorkflowSpec) -> RunRecord:
        """Roll node terminal states into the Run, or escalate a stall."""
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
                )
                return await self._require_run(run.run_id)
            return run
        failures = state.failures()
        if failures:
            return await self._terminate(
                run.run_id, "failed", f"node failed: {failures[0].node_id}"
            )
        undecided = [
            node.id
            for node in definition.nodes
            if state.terminal_state(node.id)[0] == "absent"
        ]
        if undecided:
            return await self._terminate(
                run.run_id,
                "failed",
                f"stalled: nothing in flight and {undecided[0]} never became reachable",
            )
        return await self._terminate(run.run_id, "done", "all nodes complete")

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
        )
        if not flipped:
            return await self._require_run(run_id)
        await self._append(
            run_id, {"kind": "outcome", "status": status, "detail": resolution}
        )
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
        return await self._require_run(run_id)

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
                await self._runs.record_spend(
                    run.run_id, tokens=tokens, cost_usd=cost, actor_did=self._runner_did
                )
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
                raise NodeDecisionError(
                    node.id, f"artifact {artifact!r} escapes the workspace"
                )

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
        await self._runs.append_path(run_id, entry, actor_did=self._runner_did)

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
    audit_sink: AuditSink | None = None,
    on_close: Callable[[], Awaitable[None]] | None = None,
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
    definitions = DefinitionStore(
        root / "workflows",
        tier=tier,
        operator_public_key=operator_public_key,
        audit=_definition_audit_hook(sink, tier),
    )
    return WorkflowRunner(
        tasks=WorkflowTaskStore(task_store_backend, actor_did=identity.did),
        runs=WorkflowRunStore(task_store_backend),
        definitions=definitions,
        owners=RegistryOwnerResolver(registry) if registry is not None else _no_registry(),
        runner_did=identity.did,
        tier=cast(Tier, tier),
        evaluate=evaluate_predicate,
        resolve_args=resolve_node_args,
        narrator=narrator,
        audit_sink=sink,
        run_workspace_root=root / "shared",
        on_close=on_close,
    )


def _no_registry() -> _NoRegistry:
    """Warn loudly: this runner will start and then fail every run it touches."""
    logger.warning(
        "workflow runner built with no entity registry: no node owner can be "
        "resolved, so every run will fail at its first node. Pass registry= to "
        "build_workflow_runner()."
    )
    return _NoRegistry()


def _definition_audit_hook(
    sink: AuditSink, tier: str
) -> Callable[[str, dict[str, Any]], None]:
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
    """Owner resolution with no registry wired: refuse, never guess.

    A node whose owner cannot be resolved fails the run closed. Returning some
    default DID would hand another agent's identity a task row.
    """

    async def resolve_owner(self, handle: str) -> str | None:
        return None


__all__ = [
    "NodeDecisionError",
    "UnsignedWorkflowRefusedError",
    "WorkflowRunError",
    "WorkflowRunNotFoundError",
    "WorkflowRunner",
    "build_workflow_runner",
    "node_task_id",
]
