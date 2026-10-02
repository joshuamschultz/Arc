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
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from arcprompt import PromptSource, StockPromptSource

from arcskill.hub.errors import SandboxRequired
from arcskill.improver._util import read_frontmatter
from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.codepatch import apply_bundle_patch, build_bundle_view
from arcskill.improver.config import ChangeBoundConfig, ImproverConfig
from arcskill.improver.controls import (
    MIN_MANUAL_TRACES,
    Preview,
    PreviewStash,
    audit_extra,
    eval_run_result,
    gate_payload,
    regen_refusal,
    sha256_text,
    status_result,
    unified_diff,
)
from arcskill.improver.engine import SkillOptimizer
from arcskill.improver.evalgate import EvalGate, GateDecision, load_suite, no_suite_policy
from arcskill.improver.evaluator import SkillEvaluator
from arcskill.improver.gate_log import GateLog, GateRecord
from arcskill.improver.guardrails import ChangeBound, Guardrails
from arcskill.improver.lifecycle import ConsolidationCandidate, SkillLifecycle
from arcskill.improver.models import (
    BundlePatch,
    BundleView,
    Candidate,
    EvalCase,
    LifecycleEvent,
    MutationEvent,
    SkillTrace,
)
from arcskill.improver.mutate import LLMCodeMutator, LLMSkillMerger, SkillReflector
from arcskill.improver.sandbox_runner import HubEvalRunner
from arcskill.improver.seams import (
    ApprovalProvider,
    EvalRunner,
    LLMInvoker,
    Merger,
    Mutator,
    SkillRevisionRefusedError,
    SkillRevisionWriter,
    SkillWriterUnavailableError,
)
from arcskill.improver.suitegen import GenerationResult, SuiteGenerator
from arcskill.improver.trace_store import TraceStore

_logger = logging.getLogger("arcskill.improver.improver")

# SkillOpt rejected-edit buffer: how many prior rejected patches per skill are fed back
# to the mutator as negative feedback (bounded — the convergence lever SkillOpt credits).
_REJECTED_BUFFER_MAX = 5

# Why a write-bearing operation refused: no operator-anchored revision writer.
_NO_WRITER = "skill revisions are not wired: no operator-anchored revision authority on this agent"


def _fingerprint(content: bytes) -> str:
    """SHA-256 hex digest of bundle content for audit hashing."""
    return hashlib.sha256(content).hexdigest()


class SuiteTrigger(Protocol):
    """Injected suite-generation seam (SPEC-054 COMP-004).

    The improver only routes trigger events; arcagent wires an adapter over the
    concrete :class:`~arcskill.improver.suitegen.SuiteGenerator`.
    """

    async def generate(
        self, *, skill_name: str, skill_dir: Path, kind: str
    ) -> GenerationResult | None:
        """Bootstrap (``create``), add-only extend (``extend``) or operator-``regen`` a
        suite; the generation result when the trigger reports one."""
        ...


class _SuiteGeneratorTrigger:
    """Default production :class:`SuiteTrigger` over the concrete SuiteGenerator.

    ``create`` and ``extend`` both route to ``generate()`` — the generator is add-only
    by construction, so an extend is just another bounded adoption pass (REQ-106).
    Adopted anchors are committed as one operator-signed revision through the writer;
    the generator itself writes nothing.
    """

    def __init__(self, generator: SuiteGenerator, writer: SkillRevisionWriter) -> None:
        self._generator = generator
        self._writer = writer

    async def generate(
        self, *, skill_name: str, skill_dir: Path, kind: str
    ) -> GenerationResult | None:
        view = build_bundle_view(skill_name, skill_dir / "SKILL.md")
        result = await self._generator.generate(skill_name, view)
        if result.files:
            await asyncio.to_thread(
                self._writer.commit, skill_name, result.files, reason=f"golden suite {kind}"
            )
        return result


@dataclass(frozen=True)
class _Proposal:
    """One prose candidate the optimize pass proposed, with what applying it needs."""

    current_text: str
    candidate: Candidate
    seed_scores: dict[str, float]
    trace_ids: list[str]
    stop_reason: str = ""
    iterations_run: int = 0


class ArcSkillImprover:
    """Structural ``SkillAdapter``: primitive signals in, bounded gated mutation out."""

    def __init__(
        self,
        workspace: Path,
        *,
        config: ImproverConfig | None = None,
        tier: str = "personal",
        llm: LLMInvoker | None = None,
        writer: SkillRevisionWriter | None = None,
        eval_runner: EvalRunner | None = None,
        mutator: Mutator | None = None,
        merger: Merger | None = None,
        suite_generator: SuiteTrigger | None = None,
        approval_provider: ApprovalProvider | None = None,
        audit_sink: Any = None,
        agent_did: str = "",
        skill_path: Callable[[str], Path | None] | None = None,
        reload: Callable[[], None] | None = None,
        session_id: str = "",
        max_concurrent: int = 2,
        prompt_source: PromptSource | None = None,
    ) -> None:
        self._config = config or ImproverConfig()
        # SPEC-044 §8 (tier-must-flow-through-construction): tier is bound HERE, not
        # per-call, so every ChangeBound/audit stamp carries the constructed tier.
        self._tier = tier
        # The agent's overlay-aware PromptSource so an operator prompt edit takes
        # effect; standalone (no agent) → the shipped stock prompts.
        self._prompts: PromptSource = prompt_source or StockPromptSource()
        self._llm = llm
        # The one write path (W0-skill): every applied change is committed as an
        # operator-signed anchored revision. None → every write fails closed.
        self._writer = writer
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
        self._mutator: Mutator | None = mutator or (
            LLMCodeMutator(llm, prompt_source=self._prompts) if llm else None
        )
        # Consolidation merger (Curator consolidate): default to the arcllm-backed
        # merger when an LLM seam is present — same default-wiring shape as the mutator.
        self._merger: Merger | None = merger or (
            LLMSkillMerger(llm, prompt_source=self._prompts) if llm else None
        )
        # Suite trigger (SPEC-054 COMP-004): default to the production adapter over the
        # concrete SuiteGenerator when an LLM seam AND a revision writer are present —
        # adopted anchors are skill content, so no writer means no generation.
        self._suite_generator: SuiteTrigger | None = suite_generator or (
            _SuiteGeneratorTrigger(
                SuiteGenerator(
                    llm=llm,
                    runner=self._eval_runner,
                    config=self._config.suite,
                    prompt_source=self._prompts,
                ),
                writer,
            )
            if llm and writer is not None
            else None
        )
        self._skill_path = skill_path
        self._reload = reload
        self._store = TraceStore(
            workspace,
            session_id=session_id,
            capture_args=self._config.capture_args,
            tier=tier,
        )
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
            text_of=self._read_skill_text,
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Per-skill single-flight (REQ-108): one optimization pass or suite generation
        # per skill at a time; the in-flight set lets the sweep skip without blocking.
        self._skill_locks: dict[str, asyncio.Lock] = {}
        self._generating: set[str] = set()
        # Operator controls (alpha-2 P8): the gate-verdict log the read model shows,
        # and the short-lived previews an operator may apply.
        self._gate_log = GateLog(workspace)
        self._previews = PreviewStash()

    @property
    def tier(self) -> str:
        return self._tier

    def retired_skills(self) -> frozenset[str]:
        """Names of skills currently retired OR merged-away — the offering filter (REQ-043).

        Neither a retired nor a merged skill should be advertised to or loaded by the
        agent loop (a merged skill's capability lives on in its survivor). Read from the
        candidate-store manifest so it survives restarts; revive clears either state
        (lineage retained, D-8).
        """
        return frozenset(
            name
            for name in self._candidate_store.list_skills()
            if self._candidate_store.lifecycle_state(name) in ("retired", "merged")
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
        run_id: str | None = None,
        call_id: str | None = None,
        args: dict[str, Any] | None = None,
        llm_trace_id: str | None = None,
    ) -> None:
        # ``llm_trace_id`` (H-041) records the arcllm request/trace id this tool step
        # belongs to — span METADATA the read-time curation join later resolves.
        self._store.observe(
            skill_name=skill_name,
            tool_name=tool_name,
            status=status,
            error_type=error_type,
            args=args,
            llm_trace_id=llm_trace_id,
            session_id=session_id,
            run_id=run_id,
            call_id=call_id,
        )

    async def on_turn_end(
        self, *, turn: int, outcome: str, session_id: str | None = None, run_id: str | None = None
    ) -> None:
        self._store.close_turn(
            outcome=outcome, session_id=session_id, run_id=run_id, turn_number=turn
        )

    async def maybe_improve(self, *, insight: str = "", session_id: str | None = None) -> None:
        """Spawn a bounded background optimization for every over-threshold skill."""
        for skill_name, count in self._store.usage_counts.items():
            if count >= self._config.optimize_after_uses:
                self._store.reset_count(skill_name)
                self._spawn(self._optimize(skill_name, insight))

    async def sweep_suites(self) -> None:
        """Backstop (REQ-107): bootstrap suites for suite-less skills, most-used-first."""
        if self._suite_generator is None or self._skill_path is None:
            return
        if not self._config.suite.autogen:
            return
        by_usage = sorted(self._store.usage_counts.items(), key=lambda kv: kv[1], reverse=True)
        for skill_name, _count in by_usage:
            if skill_name in self._generating:
                continue
            path = self._skill_path(skill_name)
            if path is None or load_suite(path.parent):
                continue
            async with self._skill_lock(skill_name):
                await self._generate_suite(skill_name, path.parent, kind="create")

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
        Restores either a retired or a merged skill (both use the same lineage).
        """
        if not await self._authorize("skill.lifecycle.revive", "revive", skill_name, "revive"):
            return
        self._emit_lifecycle_audit(self._lifecycle.revive(skill_name))

    async def review_consolidation(self, *, turn: int) -> None:
        """Curator sweep: propose + gate consolidation of overlapping skills.

        A merge candidate is proposed by the injected :class:`Merger`, then checked
        through the SAME :class:`EvalGate` against BOTH skills' own golden suites
        (parity, not improvement — the merge must not regress either skill's behavior)
        and the SAME operator-approval ladder as any other mutation — never a hot-swap.
        A no-op when no ``Merger``/``skill_path`` seam is wired.
        """
        if self._merger is None or self._skill_path is None:
            return
        for candidate in self._lifecycle.consolidation_candidates():
            await self._propose_merge(candidate)

    async def _propose_merge(self, candidate: ConsolidationCandidate) -> None:
        if self._merger is None or self._skill_path is None:  # narrow for the type checker
            return
        path_a = self._skill_path(candidate.skill_a)
        path_b = self._skill_path(candidate.skill_b)
        if path_a is None or path_b is None:
            return
        view_a = build_bundle_view(candidate.skill_a, path_a)
        view_b = build_bundle_view(candidate.skill_b, path_b)
        patch = await self._merger.propose(a=view_a, b=view_b, insight=candidate.reason)
        merged_bytes = patch.files.get("SKILL.md") if patch is not None else None
        if not merged_bytes:
            return
        merged_text = merged_bytes.decode("utf-8")
        gate_ok = await self._merge_gate_passes(
            candidate, view_a, view_b, merged_text, path_a, path_b
        )
        if not gate_ok:
            return
        detail = f"merge {candidate.skill_b} into {candidate.skill_a}: {candidate.reason}"
        if self._writer is None:
            _logger.info("skill consolidation %s skipped: %s", candidate.skill_a, _NO_WRITER)
            return
        if not await self._authorize(
            "skill.lifecycle.consolidate", "consolidate", candidate.skill_a, detail
        ):
            return
        await self._apply_merge(
            candidate, merged_text, patch.summary if patch else "", self._writer
        )

    async def _merge_gate_passes(
        self,
        candidate: ConsolidationCandidate,
        view_a: BundleView,
        view_b: BundleView,
        merged_text: str,
        path_a: Path,
        path_b: Path,
    ) -> bool:
        """Both originals' golden suites must accept the merged text (parity, not
        improvement — ``require_improvement=False``): a merge preserves behavior, it
        does not need to fix a failing case to be safe to apply."""
        gate = EvalGate(self._eval_runner, min_golden_cases=self._config.min_golden_cases)
        for name, before, skill_dir in (
            (candidate.skill_a, view_a, path_a.parent),
            (candidate.skill_b, view_b, path_b.parent),
        ):
            decision = await gate.decide(
                before=before,
                after=BundleView(name, merged_text, skill_dir),
                cases=load_suite(skill_dir),
                tier=self._tier,
                kind="merge",
                require_improvement=False,
            )
            if not decision.accepted:
                _logger.info(
                    "skill consolidation %s<-%s rejected on %s's suite: %s",
                    candidate.skill_a,
                    candidate.skill_b,
                    name,
                    decision.reason,
                )
                return False
        return True

    async def _apply_merge(
        self,
        candidate: ConsolidationCandidate,
        merged_text: str,
        summary: str,
        writer: SkillRevisionWriter,
    ) -> None:
        """Commit the merged text to the survivor, mark the absorbed skill merged, audit."""
        await asyncio.to_thread(
            apply_bundle_patch,
            candidate.skill_a,
            BundlePatch(files={"SKILL.md": merged_text.encode("utf-8")}, summary=summary),
            writer=writer,
            reason=f"merge {candidate.skill_b} into {candidate.skill_a}",
        )
        prior_active = self._candidate_store.load_manifest(candidate.skill_a).get(
            "active_candidate_id"
        )
        generation = self._guardrails.get_generation(candidate.skill_a) + 1
        new_candidate = Candidate(
            id=uuid.uuid4().hex,
            text=merged_text,
            parent_id=prior_active if isinstance(prior_active, str) else None,
            generation=generation,
        )
        # Registered as a normal candidate version (SAME timeline/diff/rollback surface
        # as a prose or code mutation) — an operator inspects and can roll it back
        # exactly like any other applied change.
        self._candidate_store.save(candidate.skill_a, new_candidate, active=True, frontier=True)
        self._guardrails.set_generation(candidate.skill_a, generation)
        merge_event = self._lifecycle.merge(
            candidate.skill_b, into=candidate.skill_a, reason=candidate.reason
        )
        self._emit_lifecycle_audit(merge_event)
        self._emit_audit(
            candidate.skill_a,
            "skill.mutation.applied",
            "applied",
            extra={
                "merged_from": candidate.skill_b,
                "candidate_id": new_candidate.id,
                "summary": summary,
            },
        )
        if self._reload is not None:
            self._reload()

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

    def _skill_lock(self, skill_name: str) -> asyncio.Lock:
        lock = self._skill_locks.get(skill_name)
        if lock is None:
            lock = self._skill_locks[skill_name] = asyncio.Lock()
        return lock

    async def _generate_suite(self, skill_name: str, skill_dir: Path, kind: str) -> None:
        """One guarded suite generation; the caller holds the skill's lock.

        ``create`` is idempotent (skipped once anchors exist); the in-flight set is
        what stops the sweep double-claiming a generation already running (REQ-108).
        """
        if self._suite_generator is None or not self._config.suite.autogen:
            return
        if kind == "create" and load_suite(skill_dir):
            return
        self._generating.add(skill_name)
        try:
            await self._suite_generator.generate(
                skill_name=skill_name, skill_dir=skill_dir, kind=kind
            )
        finally:
            self._generating.discard(skill_name)

    async def _optimize(self, skill_name: str, insight: str) -> None:
        """Serialize the whole pass — suite generation included — per skill (REQ-108)."""
        async with self._skill_lock(skill_name):
            await self._optimize_pass(skill_name, insight)

    async def _optimize_pass(self, skill_name: str, insight: str) -> None:
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
        # Lazy bootstrap (REQ-101): a suite-less skill gets its golden suite generated
        # before any gate decision, so acceptance is decided on anchors, not policy.
        await self._generate_suite(skill_name, skill_path.parent, kind="create")

        # Code-repair path (SPEC-044 P4): a skill with scripts + a golden suite whose
        # failing traces carry code error signals is repaired as bounded, gated,
        # re-signed code — not prose (D-1c, D-5). Otherwise fall through to prose.
        if self._should_repair_code(skill_path, traces):
            await self._optimize_code(skill_name, skill_path, traces, insight)
            return

        # Prose path needs the eval LLM (judge + reflector); code path used the mutator.
        proposal = await self._propose_prose(skill_name, skill_path, traces, persist_seed=True)
        if proposal is None:
            return
        result = await self._apply_proposal(skill_name, skill_path, proposal, source="auto")
        if result["status"] == "applied" and self._config.suite.extend_after_mutation:
            # Post-mutation extension (REQ-106): add-only anchors covering the new prose;
            # adopted anchor files are never rewritten (the generator owns add-only).
            await self._generate_suite(skill_name, skill_path.parent, kind="extend")

    def _prose_engine(self, llm: LLMInvoker) -> SkillOptimizer:
        """The judge + reflector optimizer the prose path runs."""
        return SkillOptimizer(
            config=self._config,
            evaluator=SkillEvaluator(self._config, llm=llm, prompt_source=self._prompts),
            reflector=SkillReflector(self._config, llm=llm, prompt_source=self._prompts),
            guardrails=self._guardrails,
            store=self._candidate_store,
            writer=self._writer,
        )

    async def _propose_prose(
        self, skill_name: str, skill_path: Path, traces: list[SkillTrace], *, persist_seed: bool
    ) -> _Proposal | None:
        """Run the optimize pass; the best non-seed candidate, or ``None``.

        Shared by the usage-triggered pass and the operator's improve-now, so both
        propose exactly the same way. ``persist_seed=False`` keeps a preview write-free.
        """
        if self._llm is None:
            return None
        try:
            current_text = skill_path.read_text(encoding="utf-8")
        except OSError:
            return None
        result = await self._prose_engine(self._llm).optimize(
            skill_name, current_text, traces, persist_seed=persist_seed
        )
        if result is None or result.best_candidate.id == "seed":
            return None
        return _Proposal(
            current_text=current_text,
            candidate=result.best_candidate,
            seed_scores=result.seed_scores,
            trace_ids=[t.trace_id for t in traces],
            stop_reason=result.stop_reason,
            iterations_run=result.iterations_run,
        )

    async def _apply_proposal(
        self, skill_name: str, skill_path: Path, proposal: _Proposal, *, source: str
    ) -> dict[str, Any]:
        """Gate → authorize → apply one prose candidate; every outcome is gate-logged.

        HARD GATE (REQ-022): the golden-task suite decides acceptance; the judge only
        ranked the frontier. Then the operator-approval ladder (D-10) plus the
        judge-gate guard (H-041c) in ``_authorize`` — fail-closed when approval is
        required but unwired. Only then ``apply_result`` writes, signs and audits.
        """
        if self._llm is None:  # a proposal needs the eval model; narrow for the type checker
            return status_result("unavailable", skill_name, "improvement needs the eval model")
        if self._writer is None:
            return status_result("unavailable", skill_name, _NO_WRITER)
        candidate = proposal.candidate
        decision = await self._gate(skill_name, skill_path, proposal.current_text, candidate.text)
        gate = gate_payload(decision)
        if not decision.accepted:
            _logger.info(
                "skill %s candidate rejected by eval gate: %s", skill_name, decision.reason
            )
            self._log_gate(skill_name, source, "prose", decision, "rejected", candidate.id)
            return {**status_result("rejected", skill_name, decision.reason), "gate": gate}
        if not await self._authorize(
            "skill.mutation",
            "prose",
            skill_name,
            decision.reason,
            cases=load_suite(skill_path.parent),
        ):
            self._log_gate(skill_name, source, "prose", decision, "denied", candidate.id)
            reason = "operator approval is required and was not granted"
            return {**status_result("denied", skill_name, reason), "gate": gate}
        try:
            revision = await asyncio.to_thread(
                self._prose_engine(self._llm).apply_result,
                skill_name,
                candidate,
                skill_path=skill_path,
                seed_scores=proposal.seed_scores,
                trace_ids=proposal.trace_ids,
            )
        except (SkillWriterUnavailableError, SkillRevisionRefusedError) as exc:
            self._log_gate(skill_name, source, "prose", decision, "refused", candidate.id)
            reason = f"the signed revision was refused: {exc}"
            return {**status_result("refused", skill_name, reason), "gate": gate}
        self._guardrails.set_generation(skill_name, candidate.generation)
        self._log_gate(skill_name, source, "prose", decision, "applied", candidate.id)
        if self._reload is not None:
            self._reload()
        return {
            **status_result("applied", skill_name, decision.reason),
            "candidate_id": candidate.id,
            "generation": candidate.generation,
            "revision": revision,
            "gate": gate,
        }

    def _log_gate(
        self,
        skill_name: str,
        source: str,
        kind: str,
        decision: GateDecision,
        outcome: str,
        candidate_id: str = "",
    ) -> None:
        self._gate_log.record(
            GateRecord(
                skill_name=skill_name,
                source=source,
                kind=kind,
                accepted=decision.accepted,
                reason=decision.reason,
                outcome=outcome,
                candidate_id=candidate_id,
                before_pass=decision.before_pass,
                after_pass=decision.after_pass,
                newly_passing=decision.newly_passing,
            )
        )

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
            self._log_gate(skill_name, "auto", "code", decision, "rejected")
            return
        # Operator-approval gate (D-10) + judge-gate guard (H-041c): enterprise + federal
        # approve code mutations; and at ANY tier a judge_rubric case in the suite forces the
        # review route (the sandbox can't score it — it waved through the gate unevaluated).
        # Both rules live in _authorize; here we just hand it the same suite the gate saw.
        # Fail-closed if required but unwired — never apply model-authored code unapproved.
        if not await self._authorize(
            "skill.mutation", "code", skill_name, patch.summary, cases=cases
        ):
            self._log_gate(skill_name, "auto", "code", decision, "denied")
            return
        if self._writer is None:
            self._log_gate(skill_name, "auto", "code", decision, "refused")
            _logger.info("skill %s code patch not applied: %s", skill_name, _NO_WRITER)
            return
        try:
            await asyncio.to_thread(apply_bundle_patch, skill_name, patch, writer=self._writer)
        except ValueError as exc:
            self._log_gate(skill_name, "auto", "code", decision, "refused")
            self._emit_audit(skill_name, "skill.mutation.refused", str(exc))
            return
        self._log_gate(skill_name, "auto", "code", decision, "applied")
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

        federal → every mutation + retire/revive/consolidate; enterprise → code
        mutations + consolidate (a merge touches two skills' applied bodies — the same
        integrity class as code); personal → never (auto + audit).
        ``kind`` ∈ {code, prose, retire, revive, consolidate}.
        """
        if self._tier == "federal":
            return True
        if self._tier == "enterprise":
            return kind in ("code", "consolidate")
        return False

    @staticmethod
    def _judge_review_reason(cases: list[EvalCase]) -> str | None:
        """H-041c: the HONEST reason a suite must route to review, or ``None`` if it needn't.

        A ``judge_rubric`` case needs a semantic LLM verdict the deterministic sandbox
        :class:`EvalRunner` cannot produce — so it passes unchanged BEFORE and AFTER a
        candidate, unable to cause OR prevent auto-promotion. A suite with any judge case
        therefore must NOT auto-promote ("never automatic-AND-gated at once"): the candidate
        routes to operator review carrying WHY — the count + identity of the cases that
        forced it, and the command to compute their real verdicts. The judge is NOT run
        here (no LLM at auto-promote time) — no verdict is fabricated; the reason states the
        cases need judgment the auto gate cannot give.
        """
        judged = sorted(c.id for c in cases if c.gate_type == "judge_rubric")
        if not judged:
            return None
        return (
            f"{len(judged)} judge_rubric case(s) require semantic judgment the auto gate "
            f"cannot evaluate ({', '.join(judged)}) — run `arc skill evals judge <candidate>` "
            "for the real pinned-judge verdicts"
        )

    async def _authorize(
        self,
        action: str,
        kind: str,
        skill_name: str,
        detail: str,
        *,
        cases: list[EvalCase] | None = None,
    ) -> bool:
        """Gate a consequential transition through the operator-approval ladder (D-10).

        Returns ``True`` to proceed. **Fail-closed**: when approval is required but no
        approver is wired, the action is blocked. Every decision that reaches an approver
        (or is blocked for lack of one) is an operator-signed audit event.

        ``cases`` (H-041c, mutation promote only) is the candidate's golden suite: if it
        carries any ``judge_rubric`` case, the operator-review route is FORCED regardless of
        the tier ladder, and the honest reason is folded into ``detail`` so the pending-review
        outcome states WHY and how to get the real verdicts. One place, single-sourced — the
        promote callers only supply their suite.
        """
        review_reason = self._judge_review_reason(cases) if cases is not None else None
        force_review = review_reason is not None
        if review_reason is not None:
            detail = f"{review_reason}; {detail}" if detail else review_reason
            _logger.info(
                "skill %s: %s — routing candidate to operator review (auto-promotion disallowed)",
                skill_name,
                review_reason,
            )
        if not force_review and not self._approval_required(kind):
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
        action = {
            "active": "skill.lifecycle.revived",
            "merged": "skill.lifecycle.merged",
        }.get(event.to_state, "skill.lifecycle.retired")
        self._emit_audit(event.skill_name, action, event.to_state, extra=event.to_dict())

    def _read_skill_text(self, skill_name: str) -> str | None:
        """Current SKILL.md text for ``skill_name``, or ``None`` when unresolvable.

        The Curator consolidate signal (:meth:`SkillLifecycle.consolidation_candidates`)
        reads live text, not a stored candidate — it compares what's actually loaded.
        """
        if self._skill_path is None:
            return None
        path = self._skill_path(skill_name)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

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

    # -- operator controls (alpha-2 P8) ---------------------------------------
    #
    # improve-now, eval run and regen: operator-initiated, single-flight per skill
    # (a pass already running answers ``busy``), bounded by ``manual_timeout_s``, and
    # each audited on the improver's WORM chain. Improve-now reuses the SAME optimize
    # pass, EvalGate, _authorize and apply_result the usage trigger runs.

    async def improve_now(
        self, *, skill_name: str, dry_run: bool, preview_id: str | None = None
    ) -> dict[str, Any]:
        """Run one improvement pass now; a dry run returns the diff + gate verdict only.

        ``dry_run=True`` stops before ``apply_result`` and writes nothing; the result
        carries a ``preview_id``. Applying with that id re-checks that the skill is
        unchanged, re-runs the eval gate and authorization, then applies the
        previewed candidate. Applying without one runs a fresh pass and applies it.
        """
        action = "skill.improve_now.preview" if dry_run else "skill.improve_now.apply"
        target = self._control_target(skill_name)
        if isinstance(target, dict):
            result = target
        else:
            result = await self._bounded(
                skill_name, self._improve_now(skill_name, target, dry_run, preview_id)
            )
        self._emit_audit(skill_name, action, str(result["status"]), extra=audit_extra(result))
        return result

    async def run_evals(self, *, skill_name: str) -> dict[str, Any]:
        """Run the skill's golden suite now against its current bundle; pass/fail per case."""
        target = self._control_target(skill_name)
        if isinstance(target, dict):
            result = target
        else:
            result = await self._bounded(skill_name, self._run_evals(skill_name, target))
        self._emit_audit(
            skill_name, "skill.evals.run", str(result["status"]), extra=audit_extra(result)
        )
        return result

    async def regen_evals(self, *, skill_name: str) -> dict[str, Any]:
        """Regenerate the machine-authored golden anchors for real (needs the eval model).

        Refused when a person edited the generated file. The adoption cascade is the
        same one the automatic bootstrap runs; if no candidate survives it the
        existing suite is kept (``no_change``).
        """
        target = self._control_target(skill_name)
        if isinstance(target, dict):
            result = target
        elif self._writer is None:
            result = status_result("unavailable", skill_name, _NO_WRITER)
        elif self._suite_generator is None:
            result = status_result(
                "unavailable", skill_name, "suite generation needs the eval model"
            )
        else:
            refusal = regen_refusal(skill_name, target.parent)
            result = refusal or await self._bounded(skill_name, self._regen(skill_name, target))
        self._emit_audit(
            skill_name, "skill.evals.regen", str(result["status"]), extra=audit_extra(result)
        )
        return result

    def _control_target(self, skill_name: str) -> Path | dict[str, Any]:
        """The skill's SKILL.md, or a ``not_found`` result (before any lock is made)."""
        path = self._skill_path(skill_name) if self._skill_path is not None else None
        if path is None or not path.is_file():
            return status_result("not_found", skill_name, "no such skill on this agent")
        return path

    async def _bounded(
        self, skill_name: str, work: Coroutine[Any, Any, dict[str, Any]]
    ) -> dict[str, Any]:
        """Single-flight per skill + wall-clock ceiling for an operator control."""
        lock = self._skill_lock(skill_name)
        if lock.locked():
            work.close()
            return status_result("busy", skill_name, "an improvement pass is already running")
        async with lock:
            try:
                return await asyncio.wait_for(work, timeout=self._config.manual_timeout_s)
            except TimeoutError:
                reason = f"stopped after {self._config.manual_timeout_s:g}s"
                return status_result("timeout", skill_name, reason)

    async def _improve_now(
        self, skill_name: str, skill_path: Path, dry_run: bool, preview_id: str | None
    ) -> dict[str, Any]:
        if not dry_run and preview_id is not None:
            return await self._apply_preview(skill_name, skill_path, preview_id)
        traces = self._store.load_traces(skill_name)
        refusal = self._improve_refusal(skill_name, len(traces))
        if refusal is not None:
            return refusal
        proposal = await self._propose_prose(
            skill_name, skill_path, traces, persist_seed=not dry_run
        )
        if proposal is None:
            reason = "the optimize pass found no candidate better than the current skill"
            return status_result("no_candidate", skill_name, reason)
        if not dry_run:
            return await self._apply_manual(skill_name, skill_path, proposal)
        return await self._preview(skill_name, skill_path, proposal)

    def _improve_refusal(self, skill_name: str, trace_count: int) -> dict[str, Any] | None:
        """Why an improve-now cannot run, or ``None``. Exempt tags hold for operators too."""
        if self._llm is None:
            return status_result("unavailable", skill_name, "improvement needs the eval model")
        state = self._candidate_store.lifecycle_state(skill_name)
        if state in ("retired", "merged"):
            return status_result("retired", skill_name, f"skill is {state}; revive it first")
        exempt = set(self._config.exempt_tags) & set(self._skill_tags(skill_name))
        if exempt:
            reason = f"tagged {', '.join(sorted(exempt))}; never auto-improved"
            return status_result("exempt", skill_name, reason)
        if trace_count < MIN_MANUAL_TRACES:
            reason = f"needs at least {MIN_MANUAL_TRACES} usage traces (has {trace_count})"
            return status_result("insufficient_traces", skill_name, reason)
        return None

    async def _preview(
        self, skill_name: str, skill_path: Path, proposal: _Proposal
    ) -> dict[str, Any]:
        """Gate the candidate without applying it; stash it for a later apply."""
        candidate = proposal.candidate
        decision = await self._gate(skill_name, skill_path, proposal.current_text, candidate.text)
        preview_id = self._previews.put(
            Preview(
                skill_name=skill_name,
                base_sha256=sha256_text(proposal.current_text),
                candidate=candidate,
                seed_scores=proposal.seed_scores,
                trace_ids=proposal.trace_ids,
            )
        )
        needs_review = self._approval_required("prose") or (
            self._judge_review_reason(load_suite(skill_path.parent)) is not None
        )
        return {
            **status_result("preview", skill_name, decision.reason),
            "preview_id": preview_id,
            "candidate_id": candidate.id,
            "generation": candidate.generation,
            "diff": unified_diff(proposal.current_text, candidate.text),
            "scores": dict(candidate.aggregate_scores),
            "seed_scores": dict(proposal.seed_scores),
            "stop_reason": proposal.stop_reason,
            "iterations_run": proposal.iterations_run,
            "gate": gate_payload(decision),
            "approval_required": needs_review,
        }

    async def _apply_preview(
        self, skill_name: str, skill_path: Path, preview_id: str
    ) -> dict[str, Any]:
        preview = self._previews.take(preview_id, skill_name)
        if preview is None:
            return status_result("preview_expired", skill_name, "preview it again, then apply")
        try:
            current = skill_path.read_text(encoding="utf-8")
        except OSError:
            return status_result("not_found", skill_name, "skill file is unreadable")
        if sha256_text(current) != preview.base_sha256:
            reason = "the skill changed since the preview; preview it again"
            return status_result("stale_preview", skill_name, reason)
        proposal = _Proposal(
            current_text=current,
            candidate=preview.candidate,
            seed_scores=preview.seed_scores,
            trace_ids=preview.trace_ids,
        )
        return await self._apply_manual(skill_name, skill_path, proposal)

    async def _apply_manual(
        self, skill_name: str, skill_path: Path, proposal: _Proposal
    ) -> dict[str, Any]:
        """Apply through the shared path; suite extension runs after, off the request."""
        result = await self._apply_proposal(skill_name, skill_path, proposal, source="manual")
        if result["status"] == "applied" and self._config.suite.extend_after_mutation:
            self._spawn(self._extend_suite(skill_name, skill_path.parent))
        return result

    async def _extend_suite(self, skill_name: str, skill_dir: Path) -> None:
        async with self._skill_lock(skill_name):
            await self._generate_suite(skill_name, skill_dir, kind="extend")

    async def _run_evals(self, skill_name: str, skill_path: Path) -> dict[str, Any]:
        cases = load_suite(skill_path.parent)
        if not cases:
            return status_result("no_suite", skill_name, "this skill has no golden cases yet")
        view = build_bundle_view(skill_name, skill_path)
        try:
            outcomes = await self._eval_runner.run(view, cases)
        except SandboxRequired as exc:
            return status_result("unavailable", skill_name, str(exc))
        return eval_run_result(skill_name, cases, outcomes)

    async def _regen(self, skill_name: str, skill_path: Path) -> dict[str, Any]:
        if self._suite_generator is None:  # narrowed by the caller; kept for the type checker
            return status_result(
                "unavailable", skill_name, "suite generation needs the eval model"
            )
        generated = await self._suite_generator.generate(
            skill_name=skill_name, skill_dir=skill_path.parent, kind="regen"
        )
        total = len(load_suite(skill_path.parent))
        if generated is not None and not generated.adopted:
            reason = "no candidate case survived the adoption cascade; the suite is unchanged"
            return {**status_result("no_change", skill_name, reason), "total": total}
        quarantined = generated.quarantined if generated is not None else []
        return {
            **status_result("completed", skill_name),
            "total": total,
            "adopted": len(generated.adopted) if generated is not None else None,
            "quarantined": [{"nodeid": q.nodeid, "reason": q.reason} for q in quarantined],
        }

    # -- operator-facing golden curation (H-041) -----------------------------
    #
    # ONE curation operation, wired here so the CLI (`arc skill evals promote`) and
    # arcui ("promote to golden") both drive the SAME emit path (locked design §6).
    # Imports are lazy so this block stays independent of the module's import head.

    def trace_join(self, payload_source: Any) -> Any:
        """Build the read-time trace join over this improver's spans (H-041).

        ``payload_source`` is an ``async (trace_id) -> record|None`` resolver bound to
        arcllm's ``JSONLTraceStore.get`` — the payloads are joined at read time, never
        copied into the improver's store (locked design §3).
        """
        from arcskill.improver.trace_join import TraceJoin

        return TraceJoin(self._store, payload_source)

    async def curatable_traces(self, skill_name: str, payload_source: Any) -> list[Any]:
        """Every span for ``skill_name`` joined to its arcllm payloads (or declared
        unavailable, with a reason — never a silent empty)."""
        from arcskill.improver.trace_join import TraceJoin

        join = TraceJoin(self._store, payload_source)
        return list(await join.curatable(skill_name))

    def curate_golden(self, case: Any, *, redactor: Any = None) -> Any:
        """Emit ``case`` as a signed + redacted golden under the skill's ``evals/``.

        Fail-closed: a ``judge_rubric`` case without judge id + rubric sha256 raises
        before anything is written (locked design §2).
        """
        from arcskill.improver.curation import emit_golden_case

        if self._writer is None:
            raise SkillWriterUnavailableError(_NO_WRITER)
        if self._skill_path is None:
            raise RuntimeError("no skill_path resolver wired; cannot locate the skill's evals/")
        path = self._skill_path(case.skill_name)
        if path is None:
            raise RuntimeError(f"no such skill on disk: {case.skill_name}")
        return emit_golden_case(
            path.parent,
            case,
            writer=self._writer,
            redactor=redactor,
            audit_sink=self._audit_sink,
            actor_did=self._agent_did,
            tier=self._tier,
        )

    async def evaluate_curated(
        self, skill_name: str, candidate_output: str, *, judge: Any = None
    ) -> list[Any]:
        """Compare a candidate output against every curated case PER its gate type."""
        from arcskill.improver.curation import load_curated_cases
        from arcskill.improver.goldencase import evaluate_curated_case

        if self._skill_path is None:
            return []
        path = self._skill_path(skill_name)
        if path is None:
            return []
        verdicts = []
        for case in load_curated_cases(path.parent):
            verdicts.append(
                await evaluate_curated_case(
                    case, candidate_output, judge=judge or self._llm, prompt_source=self._prompts
                )
            )
        return verdicts

    async def aclose(self) -> None:
        """Await in-flight improvement tasks (graceful shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


__all__ = ["ArcSkillImprover", "SuiteTrigger"]
