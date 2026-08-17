# Salvage notes — arcagent's deleted `modules/planning/`

`packages/arcagent/src/arcagent/modules/planning/` (SPEC-040/043, the
LLM-driven single-agent DAG planner) was deleted because it was never
enabled (`PlanningConfig.enabled` defaulted to `False`, no blueprint or
team config turned it on) and is being superseded by this `dynamic`
strategy, which does the same job with real control flow.

Before deletion, four pieces of it were good and must not be lost. Each is
quoted below from the last commit before removal (`git log -- packages/
arcagent/src/arcagent/modules/planning/` to recover full history/context if
needed). Port whatever applies to the `dynamic` strategy's own persistence,
audit, grounding, and budget-accounting needs.

---

## 1. Operator-signed integrity sidecar (`store.py`)

Problem it solves: a plan's checkpoint file lives in the agent's own
workspace. An agent with write/bash tool access could forge its own step
results (flip a step FAILED->SUCCEEDED, inject steps) and a resume would
trust the tampered file. The fix signs every checkpoint with the
**operator's** key (never the agent's own DID — the audited subject must
not be its own audit authority) into a sidecar *outside* the
agent-writable workspace, and fails closed on load if the signature is
missing or doesn't verify.

Sidecar location: `<agent_root>/.audit/plans/<plan_id>.sig`, i.e.
`plans_dir.parent.parent / ".audit" / "plans"` — two levels above the
`plans/` directory the agent's file tools can reach.

```python
class PlanStore:
    """Load/save plans durably and audit every transition.

    ``operator_signer`` closes the F4 gap: the ``plans/<id>.json`` file lives in
    the agent-writable workspace, so an agent with write/bash could flip a step
    FAILED->SUCCEEDED or inject steps and a resume would trust it. When a signer
    is configured, every checkpoint is Ed25519-signed into an operator-owned
    sidecar (beside the workspace, not inside it) and :meth:`load` re-verifies
    it — a tampered or unsigned plan file fails closed (ASI06, SI-7).
    """

    def __init__(
        self,
        plans_dir: Path,
        *,
        audit_sink: AuditSink | None = None,
        operator_signer: Any = None,
        telemetry: Any = None,
        actor_did: str = "",
    ) -> None:
        self._dir = plans_dir
        self._audit_sink = audit_sink
        self._signer = operator_signer
        # Operator-owned integrity store: <agent_root>/.audit/plans, outside the
        # agent's workspace-confined file tools' reach.
        self._sig_dir = plans_dir.parent.parent / ".audit" / "plans"
        self._telemetry = telemetry
        self._actor_did = actor_did or "did:arc:planner"

    def _sig_path(self, plan_id: str) -> Path:
        return self._sig_dir / f"{_safe_plan_id(plan_id)}.sig"

    def save(self, plan: Plan, *, action: str, target: str = "", outcome: str = "ok",
              extra: dict[str, Any] | None = None) -> None:
        path = self._path(plan.plan_id)
        plan.touch()
        body = plan.model_dump_json(indent=2)
        atomic_write_text(path, body)
        self._sign(plan.plan_id, body)
        self._emit(plan, action=action, target=target or plan.plan_id,
                    outcome=outcome, extra=extra)

    def load(self, plan_id: str) -> Plan:
        """Read a plan, rejecting a corrupt OR tampered file (fail-closed)."""
        path = self._path(plan_id)
        if not path.exists():
            raise FileNotFoundError(f"no plan at {path}")
        raw = path.read_text(encoding="utf-8")
        self._verify(plan_id, raw)
        try:
            return Plan.model_validate_json(raw)
        except ValidationError as exc:
            raise PlanIntegrityError(f"plan {plan_id!r} is malformed") from exc

    def _sign(self, plan_id: str, body: str) -> None:
        """Write the operator signature of the plan file to the sidecar."""
        if self._signer is None:
            return
        self._sig_dir.mkdir(parents=True, exist_ok=True)
        signature = self._signer.sign(body.encode("utf-8"))
        atomic_write_text(self._sig_path(plan_id), signature.hex())

    def _verify(self, plan_id: str, raw: str) -> None:
        """Fail closed unless the plan file matches its operator signature."""
        if self._signer is None:
            return
        from arctrust.signer import verify_signature

        sig_path = self._sig_path(plan_id)
        if not sig_path.exists():
            raise PlanIntegrityError(
                f"plan {plan_id!r} has no operator signature — refusing to trust it"
            )
        try:
            signature = bytes.fromhex(sig_path.read_text(encoding="utf-8").strip())
        except ValueError as exc:
            raise PlanIntegrityError(f"plan {plan_id!r} signature is malformed") from exc
        if not verify_signature(
            self._signer.algorithm, raw.encode("utf-8"), signature, self._signer.public_key
        ):
            raise PlanIntegrityError(
                f"plan {plan_id!r} failed operator signature verification — tampered"
            )
```

Note `_signer is None` is a graceful no-op both ways (sign and verify) — at
a tier with no operator authority the sidecar is simply not written or
checked, so the same code path runs personal-tier and federal-tier.

---

## 2. WORM audit sink construction (`_runtime.py`)

Wires the planner's tamper-evident, hash-chained audit trail. Signed
through the **operator** `Signer` (again, never the agent's own DID). The
chain lives beside the workspace (`<agent_root>/.audit/planning.worm`),
not inside it, for the same reason as the sidecar above. A pre-existing
chain is integrity-checked at load time, and the whole thing is
fail-open at setup (a broken audit sink degrades to "no audit," it never
blocks agent startup) but fail-loud once broken (`_logger.error` +
telemetry event) so the load-time failure isn't silent.

```python
def _build_worm_sink(workspace: Path, operator_signer: Any | None, telemetry: Any) -> Any:
    """Build the planner's operator-signed WORM audit sink (AU-9(2), SI-7(7)).

    Signed through the OPERATOR ``Signer`` (never the agent DID — the audited
    subject must not be its own audit authority). The chain lives in
    ``<agent_root>/.audit`` — beside the workspace, not inside it — so the
    agent's workspace-confined file tools cannot truncate or forge their own
    audit record. A pre-existing chain is integrity-checked on load.

    Fail-open (AU-5): if the sink cannot be opened, audit degrades to disabled
    rather than breaking module startup.
    """
    if operator_signer is None:
        return None
    try:
        from arctrust import WormSink

        chain = workspace.parent / ".audit" / "planning.worm"
        preexisting = chain.exists()
        sink = WormSink(chain, operator_signer)
    except Exception:  # reason: fail-open — never break startup on audit setup
        _logger.warning("planning WORM audit sink unavailable; audit disabled")
        return None
    if preexisting and not sink.verify_chain():
        _logger.error("planning WORM audit chain failed load-time verification")
        if telemetry is not None:
            telemetry.audit_event("planning.audit.chain_verify_failed", {"chain": str(chain)})
    return sink
```

Called from `configure()` at agent startup with
`audit_sink=_build_worm_sink(ws, operator_signer, telemetry)`, threaded
into the store alongside a `telemetry` mirror so transitions still reach
the live observable trail even when the WORM sink is absent.

---

## 3. Grounding refusal for protected identity paths (`decomposer.py`)

Goal-hijack defence (ASI01): any plan step whose description or tool hint
references `identity.md` or `policy.md` is refused **before it is ever
persisted** — decomposition raises rather than producing an ungrounded
or self-modifying plan. This is a separate, earlier check than the
runtime protected-path denylist (SPEC-035) which enforces the same thing
at *write* time; this one exists so a hijacking plan is never even
constructed.

```python
# Identity artifacts a plan may never target (ASI01). The runtime protected-path
# denylist (SPEC-035) enforces this at write time; we reject earlier, at plan
# construction, so an ungrounded plan is never even persisted.
_PROTECTED_NAMES = ("identity.md", "policy.md")


class DecompositionError(ValueError):
    """Raised when the model returns an ungrounded, malformed, or hijacking plan."""


def _targets_protected_path(step: PlanStep) -> bool:
    haystack = f"{step.description} {step.tool_hint or ''}".lower()
    return any(name in haystack for name in _PROTECTED_NAMES)


def _ground(steps: Sequence[PlanStep], known_tools: Iterable[str]) -> None:
    """Reject ungrounded or goal-hijacking plans (REQ-005/040)."""
    if not steps:
        raise DecompositionError("plan has no steps")
    for step in steps:
        if _targets_protected_path(step):
            raise DecompositionError(
                f"step {step.step_id!r} targets a protected identity path — refused"
            )
    known = set(known_tools)
    hints = [s.tool_hint for s in steps if s.tool_hint]
    if known and hints and not any(hint in known for hint in hints):
        raise DecompositionError("plan references no known capability — cannot be grounded")
```

`_ground` is called on every freshly decomposed plan AND every replan
(the remainder produced by a Reflexion-lite revision on step failure), so
a hijack attempt during replan is caught the same way as at initial
decomposition.

---

## 4. Reserve-then-settle budget accounting under a lock (`executor.py`)

`ConcurrentStepExecutor` runs a whole ready frontier of independent steps
concurrently against one shared plan-level budget. The problem: naive
"check remaining budget, then spend" has a TOCTOU race under concurrency
— N branches can each see headroom and together overspend. The fix is
**reserve-then-settle**: each branch reserves its worst-case cap from the
shared budget *before* it launches (under an `asyncio.Lock`, so
reservations serialize even though the branches' actual runs are
concurrent), and settles its real spend back against the reservation on
completion — success, captured failure, or cancellation all go through
the same `finally`-protected settle path so a reservation can never leak.

```python
# A budget grant: the (tokens, cost) ceiling reserved for one branch. ``None`` on
# a dimension means unbounded (that dimension has no plan ceiling).
BudgetGrant = tuple["int | None", "float | None"]


class ConcurrentStepExecutor:
    """Concurrent Plan-Execute executor — SPEC-043 REQ-053/054/055/056.

    Runs the whole ready frontier concurrently while holding the plan aggregate
    budget with **reserve-then-settle**: each branch reserves its cap from the
    shared budget BEFORE launch (under a plan-level lock) and settles actual spend
    on completion, so ``Σ(reservations + spend) ≤ Plan.budget`` — N concurrent
    branches can never overspend (LLM10).
    """

    def __init__(
        self,
        run_fn: RunFn,
        *,
        actor_did: str = "",
        max_parallel: int = 8,
        step_max_tokens: int | None = None,
        step_max_cost: float | None = None,
    ) -> None:
        self._run_fn = run_fn
        self._actor_did = actor_did
        self._max_parallel = max_parallel
        self._step_max_tokens = step_max_tokens
        self._step_max_cost = step_max_cost
        self._budget_lock = asyncio.Lock()

    async def run_ready(self, steps: list[PlanStep], *, plan: Plan) -> list[StepOutcome]:
        """Dispatch the independent ready frontier concurrently, gated + bounded.

        Reservations are placed sequentially (each sees prior decrements) so the
        aggregate can never be over-committed; a step that cannot reserve any
        headroom is DEFERRED (left PENDING for a later pass). The reserved
        branches then run concurrently through the wired primitive and settle on
        completion. Returns outcomes for the steps that RAN, in submission order.
        """
        reserved: list[tuple[PlanStep, BudgetGrant]] = []
        for step in steps:
            grant = await self._reserve(plan)
            if grant is None:
                break  # no headroom this pass — defer the rest (stay PENDING)
            step.attempts += 1
            reserved.append((step, grant))
        if not reserved:
            return []

        async def _run(pair: tuple[PlanStep, BudgetGrant]) -> StepOutcome:
            branch_step, branch_grant = pair
            return await self._run_branch(branch_step, plan, branch_grant)

        outcomes = await arcrun.dispatch_ready(reserved, _run, max_parallel=self._max_parallel)
        return [
            outcome
            if isinstance(outcome, StepOutcome)
            else StepOutcome(StepStatus.FAILED, failure_reason=f"run error: {outcome}")
            for outcome in outcomes
        ]

    async def _run_branch(self, step: PlanStep, plan: Plan, grant: BudgetGrant) -> StepOutcome:
        """Run one bounded branch capped at its reservation, then settle (REQ-055).

        ``_settle`` runs in a ``finally`` so the reservation is released exactly
        once on EVERY exit — success, a captured error, OR cancellation (SPEC-043
        F4). ``except Exception`` does not catch ``CancelledError``; without the
        ``finally`` a cancelled branch would leak its reservation and wedge the
        plan under-budget forever.
        """
        max_tokens, max_cost = grant
        outcome = StepOutcome(StepStatus.FAILED, failure_reason="cancelled")
        try:
            result = await self._run_fn(
                task=step.description,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
                actor_did=self._actor_did,
            )
            outcome = classify_loop_result(result)
            outcome.tokens_used = int(result.tokens_used.get("total", 0) or 0)
            outcome.cost_usd = result.cost_usd or 0.0
        except Exception as exc:  # reason: a bad branch must not kill siblings
            outcome = StepOutcome(StepStatus.FAILED, failure_reason=f"run error: {exc}")
        finally:
            await self._settle(plan, grant, outcome)
        return outcome

    async def _reserve(self, plan: Plan) -> BudgetGrant | None:
        """Reserve this branch's cap from the shared budget (under the lock).

        Returns ``None`` when no headroom remains (defer/fail the branch); else
        grants ``min(per-step cap, available)`` on each bounded dimension and
        records the reservation so a later reserve sees less (REQ-053).
        """
        async with self._budget_lock:
            avail_tok, avail_cost = plan.available_budget()
            if (avail_tok is not None and avail_tok <= 0) or (
                avail_cost is not None and avail_cost <= 0
            ):
                return None
            grant_tok = self._cap(avail_tok, self._step_max_tokens)
            grant_cost = self._cap(avail_cost, self._step_max_cost)
            plan.reserved_tokens += grant_tok or 0
            plan.reserved_cost += grant_cost or 0
            return (grant_tok, grant_cost)

    async def _settle(self, plan: Plan, grant: BudgetGrant, outcome: StepOutcome) -> None:
        """Accrue actual spend and free the reservation (under the lock)."""
        async with self._budget_lock:
            plan.tokens_spent += outcome.tokens_used
            plan.cost_spent += outcome.cost_usd
            plan.reserved_tokens -= grant[0] or 0
            plan.reserved_cost -= grant[1] or 0

    @staticmethod
    def _cap(available: Any, per_step: Any) -> Any:
        """min(per-step ceiling, available); unbounded when both are ``None``."""
        if available is None:
            return per_step
        if per_step is None:
            return available
        return min(per_step, available)
```

`plan.available_budget()` = ceiling minus (already-spent + already-
reserved) on each dimension; `plan.remaining_budget()` (used by the
non-concurrent `ArcRunStepExecutor`) is spend-only, no reservations —
the two coexist because only the concurrent path needs reservation
accounting. Both live on the `Plan` model, which is being deleted with
the rest of this module — port the ceiling/spent/reserved bookkeeping
fields too if `dynamic` adopts this pattern.
