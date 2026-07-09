"""ArcSkillImprover — the ``SkillAdapter``-shaped facade arcagent wires (SPEC-044).

Provider-free orchestration over the injected seams. Consumes the primitive per-turn
signals the arcagent extension forwards (``observe``/``on_turn_end``/``maybe_improve``/
``review_lifecycle``), collects traces, and — when a skill crosses its usage threshold —
runs the bounded, gated, signed improvement pass in a caught background task so a failing
optimization never touches the agent loop (NFR-005).

The concrete acceptance gate (golden-task eval), code-repair, change-bound, and lifecycle
sweep land in SPEC-044 Phases 3-6; this facade wires the seam end-to-end with the prose
path so the ``SkillAdapter`` contract is genuinely live, not a stub.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcskill.improver._util import read_frontmatter
from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.codepatch import apply_bundle_patch, build_bundle_view
from arcskill.improver.config import ChangeBoundConfig, ImproverConfig
from arcskill.improver.engine import SkillOptimizer
from arcskill.improver.evalgate import EvalGate, GateDecision, load_suite, no_suite_policy
from arcskill.improver.evaluator import SkillEvaluator
from arcskill.improver.guardrails import ChangeBound, Guardrails
from arcskill.improver.lifecycle import SkillLifecycle
from arcskill.improver.models import (
    BundlePatch,
    BundleView,
    LifecycleEvent,
    MutationEvent,
    SkillTrace,
)
from arcskill.improver.mutate import LLMCodeMutator, SkillReflector
from arcskill.improver.sandbox_runner import HubEvalRunner
from arcskill.improver.seams import ApprovalProvider, EvalRunner, LLMInvoker, Mutator, Signer
from arcskill.improver.trace_store import TraceStore

_logger = logging.getLogger("arcskill.improver.improver")

# SkillOpt rejected-edit buffer: how many prior rejected patches per skill are fed back
# to the mutator as negative feedback (bounded — the convergence lever SkillOpt credits).
_REJECTED_BUFFER_MAX = 5


def _fingerprint(content: bytes) -> str:
    """SHA-256 hex digest of bundle content for audit hashing."""
    return hashlib.sha256(content).hexdigest()


class ArcSkillImprover:
    """Structural ``SkillAdapter``: primitive signals in, bounded gated mutation out."""

    def __init__(
        self,
        workspace: Path,
        *,
        config: ImproverConfig | None = None,
        tier: str = "personal",
        llm: LLMInvoker | None = None,
        signer: Signer | None = None,
        eval_runner: EvalRunner | None = None,
        mutator: Mutator | None = None,
        approval_provider: ApprovalProvider | None = None,
        audit_sink: Any = None,
        agent_did: str = "",
        skill_path: Callable[[str], Path | None] | None = None,
        reload: Callable[[], None] | None = None,
        session_id: str = "",
        max_concurrent: int = 2,
    ) -> None:
        self._config = config or ImproverConfig()
        # SPEC-044 §8 (tier-must-flow-through-construction): tier is bound HERE, not
        # per-call, so every ChangeBound/audit stamp carries the constructed tier.
        self._tier = tier
        self._llm = llm
        self._signer = signer
        # Operator-approval seam (D-10). The improver decides *when* approval is required
        # per the tier ladder; the injected provider (bound to the shared HumanGate) decides
        # the answer. Fail-closed when required but unwired (federal/enterprise), so a missing
        # provider blocks, never silently applies.
        self._approval_provider = approval_provider
        # Constructed agent DID — the audit *actor* (who authored the mutation), distinct
        # from the operator key that signs the WORM chain (REQ-050).
        self._agent_did = agent_did
        # Default to the concrete sandboxed runner (SPEC-044 P3.3) when none is injected —
        # this is the production wiring; unit tests inject deterministic fakes.
        self._eval_runner: EvalRunner = eval_runner or HubEvalRunner(tier=tier)
        # Code-repair mutator (SPEC-044 P4): default to the arcllm-backed proposer when an
        # LLM seam is present; provider-free, so tests inject a deterministic Mutator.
        self._mutator: Mutator | None = mutator or (LLMCodeMutator(llm) if llm else None)
        self._skill_path = skill_path
        self._reload = reload
        self._store = TraceStore(workspace, session_id=session_id)
        self._guardrails = Guardrails(self._config)
        self._change_bound = ChangeBound(tier, self._config.change_bound)
        self._audit_sink = audit_sink
        self._candidate_store = CandidateStore(
            workspace, audit_sink=audit_sink, actor_did=agent_did
        )
        # Per-skill bounded buffer of prior rejected patches (SkillOpt rejected-edit buffer),
        # threaded back into the mutator prompt as negative feedback on the next attempt.
        self._rejected: dict[str, list[str]] = {}
        self._lifecycle = SkillLifecycle(
            self._candidate_store,
            self._config.lifecycle,
            load_traces=self._store.load_traces,
            generation_of=self._guardrails.get_generation,
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @property
    def tier(self) -> str:
        return self._tier

    def retired_skills(self) -> frozenset[str]:
        """Names of skills currently retired (disabled) — the offering filter (REQ-043).

        A retired skill must not be advertised to or loaded by the agent loop. Read from
        the candidate-store manifest so it survives restarts; revive clears it (lineage
        retained, D-8).
        """
        return frozenset(
            name
            for name in self._candidate_store.list_skills()
            if self._candidate_store.lifecycle_state(name) == "retired"
        )

    # -- SkillAdapter surface ------------------------------------------------

    async def observe(
        self,
        *,
        skill_name: str,
        tool_name: str,
        status: str,
        error_type: str | None,
        session_id: str | None = None,
    ) -> None:
        self._store.observe(
            skill_name=skill_name, tool_name=tool_name, status=status, error_type=error_type
        )

    async def on_turn_end(self, *, turn: int, outcome: str, session_id: str | None = None) -> None:
        self._store.close_turn(outcome=outcome)

    async def maybe_improve(self, *, insight: str = "", session_id: str | None = None) -> None:
        """Spawn a bounded background optimization for every over-threshold skill."""
        for skill_name, count in self._store.usage_counts.items():
            if count >= self._config.optimize_after_uses:
                self._store.reset_count(skill_name)
                self._spawn(self._optimize(skill_name, insight))

    async def review_lifecycle(self, *, turn: int) -> None:
        """Curator sweep: retire inactive/failing skills, each an audited transition (AC-5).

        Each proposed retirement is gated through the tier approval ladder (federal
        requires operator approval; fail-closed if unwired) before it commits (D-10).
        """
        for skill_name, reason in self._lifecycle.pending_retirements():
            if not await self._authorize("skill.lifecycle.retire", "retire", skill_name, reason):
                continue
            self._emit_lifecycle_audit(self._lifecycle.retire(skill_name, reason=reason))

    async def revive(self, skill_name: str) -> None:
        """Operator-initiated revive of a retired skill (REQ-044); gated + audited.

        Federal requires operator approval even for revive (D-10); fail-closed if unwired.
        """
        if not await self._authorize("skill.lifecycle.revive", "revive", skill_name, "revive"):
            return
        self._emit_lifecycle_audit(self._lifecycle.revive(skill_name))

    def rollback(self, skill_name: str, candidate_id: str) -> None:
        """Revert to a prior candidate, cool off, and operator-audit the reversal (REQ-052)."""
        self._candidate_store.rollback(skill_name, candidate_id)
        self._guardrails.set_cooloff(
            skill_name, self._store.turn_number + self._config.cooloff_turns
        )
        self._emit_audit(
            skill_name,
            "skill.mutation.rolled_back",
            "rolled_back",
            extra={"candidate_id": candidate_id},
        )

    # -- internals -----------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        """Track a background task; caught exceptions never reach the agent loop."""
        task = asyncio.create_task(self._guarded(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, coro: Any) -> None:
        async with self._semaphore:
            try:
                await coro
            except Exception:  # reason: background improvement must never crash the loop
                _logger.warning("skill improvement pass failed", exc_info=True)

    async def _optimize(self, skill_name: str, insight: str) -> None:
        if self._skill_path is None:
            return
        current_turn = self._store.turn_number
        traces = [
            t
            for t in self._store.load_traces(skill_name)
            if current_turn - t.turn_number >= self._config.trace_buffer_turns
        ]
        if not self._guardrails.check_eligible(
            skill_name,
            traces,
            current_turn=current_turn,
            skill_tags=self._skill_tags(skill_name),
        ):
            return
        skill_path = self._skill_path(skill_name)
        if skill_path is None:
            return

        # Code-repair path (SPEC-044 P4): a skill with scripts + a golden suite whose
        # failing traces carry code error signals is repaired as bounded, gated,
        # re-signed code — not prose (D-1c, D-5). Otherwise fall through to prose.
        if self._should_repair_code(skill_path, traces):
            await self._optimize_code(skill_name, skill_path, traces, insight)
            return

        # Prose path needs the eval LLM (judge + reflector); code path used the mutator.
        if self._llm is None:
            return
        try:
            current_text = skill_path.read_text(encoding="utf-8")
        except OSError:
            return

        engine = SkillOptimizer(
            config=self._config,
            evaluator=SkillEvaluator(self._config, llm=self._llm),
            reflector=SkillReflector(self._config, llm=self._llm),
            guardrails=self._guardrails,
            store=self._candidate_store,
            signer=self._signer,
        )
        result = await engine.optimize(skill_name, current_text, traces)
        if result is None or result.best_candidate.id == "seed":
            return

        # HARD GATE (REQ-022): the golden-task suite decides acceptance; the judge only
        # ranked the frontier above. A candidate applies only on strict improvement.
        candidate = result.best_candidate
        decision = await self._gate(skill_name, skill_path, current_text, candidate.text)
        if not decision.accepted:
            _logger.info(
                "skill %s candidate rejected by eval gate: %s", skill_name, decision.reason
            )
            return

        # Operator-approval gate (D-10): federal approves every mutation. Fail-closed if
        # required but unwired — a prose candidate never applies unapproved at federal.
        if not await self._authorize("skill.mutation", "prose", skill_name, decision.reason):
            return

        engine.apply_result(
            skill_name,
            candidate,
            skill_path=skill_path,
            seed_scores=result.seed_scores,
            trace_ids=[t.trace_id for t in traces],
        )
        self._guardrails.set_generation(skill_name, candidate.generation)
        if self._reload is not None:
            self._reload()

    def _should_repair_code(self, skill_path: Path, traces: list[SkillTrace]) -> bool:
        """Code-repair is eligible: mutator present, scripts + golden suite exist, and a
        failing trace carries a code error signal (``error_type``)."""
        if self._mutator is None:
            return False
        skill_dir = skill_path.parent
        has_scripts = (skill_dir / "scripts").is_dir() or (skill_dir / "src").is_dir()
        has_suite = bool(load_suite(skill_dir))
        has_error = any(tc.error_type for t in traces for tc in t.tool_calls)
        return has_scripts and has_suite and has_error

    async def _optimize_code(
        self, skill_name: str, skill_path: Path, traces: list[SkillTrace], insight: str
    ) -> None:
        """Propose → change-bound → golden-gate → re-sign → reload a code patch (AC-2)."""
        if self._mutator is None:  # guarded by _should_repair_code; narrow for the type checker
            return
        skill_dir = skill_path.parent
        current = build_bundle_view(skill_name, skill_path)
        failures = self._compose_failures(skill_name, traces)
        patch = await self._mutator.propose(
            kind="code", current=current, failures=failures, insight=insight
        )
        if patch is None or not patch.files:
            return
        # Change-bound (SkillOpt): reject an over-budget patch BEFORE the costly sandbox
        # eval, and audit the rejection (AC-4). tier flows from construction. The per-attempt
        # edit budget decays on a cosine schedule across the improve-attempts budget, so
        # early attempts explore and late ones consolidate before retirement (SDD §7.3).
        edit_budget = self._change_bound.scheduled_edits(
            self._guardrails.get_generation(skill_name),
            self._config.lifecycle.improve_attempts_before_retire,
        )
        ok, reason = self._change_bound.check(
            patch,
            current.scripts,
            skill_override=self._skill_override(skill_name),
            edit_budget=edit_budget,
        )
        if not ok:
            _logger.info("skill %s code patch over change-bound: %s", skill_name, reason)
            self._record_rejection(skill_name, patch, reason)
            self._emit_audit(skill_name, "skill.mutation.bound_rejected", reason)
            return
        cases = load_suite(skill_dir)
        after = BundleView(skill_name, current.text, skill_dir, scripts=patch.files)
        gate = EvalGate(self._eval_runner, min_golden_cases=self._config.min_golden_cases)
        decision = await gate.decide(
            before=current, after=after, cases=cases, tier=self._tier, kind="code"
        )
        if not decision.accepted:
            _logger.info("skill %s code patch rejected: %s", skill_name, decision.reason)
            self._record_rejection(skill_name, patch, decision.reason)
            return
        # Operator-approval gate (D-10): enterprise + federal approve code mutations.
        # Fail-closed if required but unwired — never apply model-authored code unapproved.
        if not await self._authorize("skill.mutation", "code", skill_name, patch.summary):
            return
        apply_bundle_patch(skill_dir, patch, signer=self._signer)
        self._audit_code_mutation(skill_name, current, patch, [t.trace_id for t in traces])
        self._guardrails.set_generation(
            skill_name, self._guardrails.get_generation(skill_name) + 1
        )
        if self._reload is not None:
            self._reload()
        _logger.info("skill %s code patch applied: %s", skill_name, patch.summary)

    def _compose_failures(self, skill_name: str, traces: list[SkillTrace]) -> str:
        """Failure summary for the mutator, augmented with the rejected-edit buffer (MED-5b).

        Feeding prior rejected patches back as negative feedback is the SkillOpt mechanism
        that stops the mutator re-proposing losing edits — the credited convergence lever.
        """
        failures = self._summarize_failures(traces)
        rejected = self._rejected.get(skill_name)
        if rejected:
            failures += "\n\nPREVIOUSLY-REJECTED EDITS (do NOT repeat these):\n" + "\n".join(
                rejected
            )
        return failures

    def _record_rejection(self, skill_name: str, patch: BundlePatch, reason: str) -> None:
        """Buffer a rejected patch as bounded negative feedback for the next attempt."""
        buf = self._rejected.setdefault(skill_name, [])
        summary = patch.summary or ", ".join(sorted(patch.files))
        buf.append(f"- tried: {summary[:160]} -> rejected: {reason}")
        del buf[:-_REJECTED_BUFFER_MAX]

    def _approval_required(self, kind: str) -> bool:
        """Whether the tier ladder (D-10 / PRD §7) requires operator approval for ``kind``.

        federal → every mutation + retire/revive; enterprise → code mutations only;
        personal → never (auto + audit). ``kind`` ∈ {code, prose, retire, revive}.
        """
        if self._tier == "federal":
            return True
        if self._tier == "enterprise":
            return kind == "code"
        return False

    async def _authorize(self, action: str, kind: str, skill_name: str, detail: str) -> bool:
        """Gate a consequential transition through the operator-approval ladder (D-10).

        Returns ``True`` to proceed. **Fail-closed**: when approval is required but no
        approver is wired, the action is blocked. Every decision that reaches an approver
        (or is blocked for lack of one) is an operator-signed audit event.
        """
        if not self._approval_required(kind):
            return True
        if self._approval_provider is None:
            self._emit_audit(
                skill_name, f"{action}.approval", "denied_no_approver", extra={"detail": detail}
            )
            return False
        approved = await self._approval_provider(action, skill_name, detail)
        self._emit_audit(
            skill_name,
            f"{action}.approval",
            "approved" if approved else "denied",
            extra={"detail": detail},
        )
        return approved

    def _summarize_failures(self, traces: list[SkillTrace]) -> str:
        """A compact error-signal summary fed to the code mutator (GEPA reflection seed)."""
        counts: dict[str, int] = {}
        for trace in traces:
            for tc in trace.tool_calls:
                if tc.error_type:
                    key = f"{tc.error_type} in {tc.tool_name}"
                    counts[key] = counts.get(key, 0) + 1
        if not counts:
            return "No explicit error types; skill underperformed on its golden suite."
        return "\n".join(f"- {sig} (x{n})" for sig, n in sorted(counts.items()))

    def _audit_code_mutation(
        self, skill_name: str, before: BundleView, patch: BundlePatch, trace_ids: list[str]
    ) -> None:
        """Emit a tier-stamped mutation audit event for an applied code patch (WORM chain)."""
        new_hash = _fingerprint(b"".join(sorted(patch.files.values())))
        event = MutationEvent(
            timestamp=datetime.now(UTC),
            skill_name=skill_name,
            previous_hash=_fingerprint(before.text.encode("utf-8")),
            new_hash=new_hash,
            candidate_id="code-patch",
            generation=self._guardrails.get_generation(skill_name) + 1,
            scores={},
            improvement={"files_touched": float(patch.files_touched)},
            stop_reason="applied",
            trace_ids=trace_ids,
        )
        self._emit_audit(
            skill_name,
            "skill.mutation.applied",
            "applied",
            payload_hash=new_hash,
            extra=event.to_dict(),
        )

    def _emit_audit(
        self,
        skill_name: str,
        action: str,
        outcome: str,
        *,
        payload_hash: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit a tier-stamped audit event (tier flows from construction — §8)."""
        if self._audit_sink is None:
            return
        from arctrust import AuditEvent, emit

        emit(
            AuditEvent(
                actor_did=self._agent_did or "did:arc:skill-improver",
                action=action,
                target=skill_name,
                outcome=outcome,
                tier=self._tier,
                payload_hash=payload_hash,
                extra=extra or {},
            ),
            self._audit_sink,
        )

    def _emit_lifecycle_audit(self, event: LifecycleEvent) -> None:
        """Emit a tier-stamped lifecycle-transition audit event (operator-signed WORM)."""
        action = (
            "skill.lifecycle.revived" if event.to_state == "active" else "skill.lifecycle.retired"
        )
        self._emit_audit(event.skill_name, action, event.to_state, extra=event.to_dict())

    def _skill_override(self, skill_name: str) -> ChangeBoundConfig | None:
        """Read a per-skill change-bound override from the skill's frontmatter, if any."""
        if self._skill_path is None:
            return None
        path = self._skill_path(skill_name)
        if path is None:
            return None
        fm = read_frontmatter(path)
        if not fm or not isinstance(fm.get("improver"), dict):
            return None
        try:
            return ChangeBoundConfig(**fm["improver"])
        except Exception:  # reason: a malformed per-skill override is ignored (tier ceiling holds)
            return None

    async def _gate(
        self, skill_name: str, skill_path: Path, before_text: str, after_text: str
    ) -> GateDecision:
        """Run the golden-task gate for a prose candidate (code path lands Phase 4)."""
        skill_dir = skill_path.parent
        cases = load_suite(skill_dir)
        if not cases:
            return no_suite_policy(self._tier, "prose")
        gate = EvalGate(self._eval_runner, min_golden_cases=self._config.min_golden_cases)
        return await gate.decide(
            before=BundleView(skill_name, before_text, skill_dir),
            after=BundleView(skill_name, after_text, skill_dir),
            cases=cases,
            tier=self._tier,
            kind="prose",
        )

    def _skill_tags(self, skill_name: str) -> list[str]:
        if self._skill_path is None:
            return []
        path = self._skill_path(skill_name)
        if path is None:
            return []
        fm = read_frontmatter(path)
        if fm is None:
            return []
        tags = fm.get("tags", [])
        return list(tags) if isinstance(tags, list) else []

    async def aclose(self) -> None:
        """Await in-flight improvement tasks (graceful shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


__all__ = ["ArcSkillImprover"]
