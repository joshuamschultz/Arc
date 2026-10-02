"""Optimization engine — orchestrates the skill improvement loop.

Coordinates trace splitting, evaluation, reflection, guardrail validation,
and Pareto frontier management to produce improved skill candidates.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path

from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.config import ImproverConfig
from arcskill.improver.evaluator import SkillEvaluator
from arcskill.improver.guardrails import Guardrails
from arcskill.improver.models import (
    Candidate,
    MutationEvent,
    OptimizeResult,
    SkillTrace,
)
from arcskill.improver.mutate import SkillReflector
from arcskill.improver.pareto import ParetoFrontier
from arcskill.improver.seams import (
    SkillRevisionRefusedError,
    SkillRevisionWriter,
    SkillWriterUnavailableError,
)

_logger = logging.getLogger("arcskill.improver.engine")


class SkillOptimizer:
    """Orchestrate the full skill optimization loop."""

    def __init__(
        self,
        config: ImproverConfig,
        evaluator: SkillEvaluator,
        reflector: SkillReflector,
        guardrails: Guardrails,
        store: CandidateStore,
        *,
        writer: SkillRevisionWriter | None = None,
    ) -> None:
        self._config = config
        self._evaluator = evaluator
        self._reflector = reflector
        self._guardrails = guardrails
        self._store = store
        # The one write path (W0-skill): an applied candidate becomes an
        # operator-signed anchored revision. None → apply refuses (fail closed).
        self._writer = writer

    def split_traces(
        self,
        traces: list[SkillTrace],
        ratio: float = 0.7,
        seed: int | None = None,
    ) -> tuple[list[SkillTrace], list[SkillTrace]]:
        """Split traces into train/holdout sets."""
        shuffled = list(traces)
        rng = random.Random(seed)  # noqa: S311 — not cryptographic, just trace shuffling
        rng.shuffle(shuffled)
        split_idx = max(1, int(len(shuffled) * ratio))
        # Ensure at least 1 holdout
        if split_idx >= len(shuffled):
            split_idx = len(shuffled) - 1
        return shuffled[:split_idx], shuffled[split_idx:]

    def sample_minibatch(
        self,
        traces: list[SkillTrace],
        size: int = 5,
    ) -> list[SkillTrace]:
        """Sample a random subset of traces for evaluation."""
        if len(traces) <= size:
            return list(traces)
        return random.sample(traces, size)

    async def optimize(
        self,
        skill_name: str,
        current_text: str,
        traces: list[SkillTrace],
        *,
        persist_seed: bool = True,
    ) -> OptimizeResult | None:
        """Run the full optimization loop.

        ``persist_seed=False`` keeps the pass write-free (an operator's dry-run
        preview): the seed snapshot is only written by a pass that may apply.
        """
        # Split traces
        train, holdout = self.split_traces(traces, ratio=0.7, seed=42)

        # Create seed candidate
        seed = Candidate(
            id="seed",
            text=current_text,
            token_count=len(current_text.split()),
        )

        # Evaluate seed on holdout
        seed_eval = await self._evaluator.evaluate(current_text, holdout)
        seed_eval.compute_aggregates()
        seed.aggregate_scores = dict(seed_eval.aggregate_scores)
        seed.scores = {
            dim: [float(ts[dim].score) for ts in seed_eval.per_trace_scores]
            for dim in seed_eval.aggregate_scores
        }

        # Initialize frontier
        frontier = ParetoFrontier()
        frontier.add(seed)

        if persist_seed:
            self._store.save_seed(skill_name, current_text)

        token_budget = int(seed.token_count * self._config.max_token_ratio)

        stagnation_count = 0
        iterations_run = 0
        stop_reason = "max_iterations"

        for i in range(self._config.max_iterations):
            iterations_run = i + 1

            # Select parent from frontier
            parent = frontier.select()

            # Evaluate parent on train minibatch
            minibatch = self.sample_minibatch(train)
            parent_eval = await self._evaluator.evaluate(parent.text, minibatch)
            parent_eval.compute_aggregates()

            # Identify failures (traces with low aggregate score)
            failures = []
            for idx, trace_scores in enumerate(parent_eval.per_trace_scores):
                avg = sum(ds.score for ds in trace_scores.values()) / max(len(trace_scores), 1)
                if avg < self._config.failure_score_threshold and idx < len(minibatch):
                    failures.append((minibatch[idx], trace_scores))

            if not failures:
                stagnation_count += 1
                if stagnation_count >= self._config.stagnation_limit:
                    stop_reason = "stagnation"
                    break
                continue

            # Reflect on failures -> constrained mutation
            mutation_text = await self._reflector.reflect(
                parent.text,
                failures,
                token_budget,
            )
            if not mutation_text:
                stagnation_count += 1
                if stagnation_count >= self._config.stagnation_limit:
                    stop_reason = "stagnation"
                    break
                continue

            # Create mutation candidate
            mutation = Candidate(
                id=str(uuid.uuid4())[:12],
                text=mutation_text,
                token_count=len(mutation_text.split()),
                parent_id=parent.id,
                generation=parent.generation + 1,
            )

            # Validate against guardrails
            if not self._guardrails.validate_candidate(mutation, seed):
                stagnation_count += 1
                if stagnation_count >= self._config.stagnation_limit:
                    stop_reason = "guardrail"
                    break
                continue

            # Evaluate mutation on holdout
            mutation_eval = await self._evaluator.evaluate(mutation.text, holdout)
            mutation_eval.compute_aggregates()
            mutation.aggregate_scores = dict(mutation_eval.aggregate_scores)
            mutation.scores = {
                dim: [float(ts[dim].score) for ts in mutation_eval.per_trace_scores]
                for dim in mutation_eval.aggregate_scores
            }

            # Add to frontier if improves
            if frontier.add_if_improves(mutation, min_delta=self._config.min_delta):
                stagnation_count = 0
            else:
                stagnation_count += 1

            if stagnation_count >= self._config.stagnation_limit:
                stop_reason = "stagnation"
                break

        # Return best
        best = frontier.overall_best()
        seed_scores = dict(seed.aggregate_scores)
        improvement = {
            dim: best.aggregate_scores.get(dim, 0) - seed_scores.get(dim, 0) for dim in seed_scores
        }

        return OptimizeResult(
            skill_name=skill_name,
            best_candidate=best,
            frontier=frontier.candidates,
            iterations_run=iterations_run,
            stop_reason=stop_reason,
            seed_scores=seed_scores,
            improvement=improvement,
        )

    def apply_result(
        self,
        skill_name: str,
        candidate: Candidate,
        *,
        skill_path: Path,
        seed_scores: dict[str, float],
        trace_ids: list[str],
    ) -> str:
        """Commit the candidate as a new skill revision, save it, and audit-log it.

        Returns the revision digest. Raises :class:`SkillWriterUnavailableError` when no
        writer is wired and :class:`SkillRevisionRefusedError` when the writer refuses;
        nothing is activated in either case.
        """
        if self._writer is None:
            raise SkillWriterUnavailableError("no operator-anchored revision writer is wired")
        # Read current text for audit
        previous_text = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
        previous_hash = Candidate(id="", text=previous_text).fingerprint

        try:
            revision = self._writer.commit(
                skill_name,
                {"SKILL.md": candidate.text.encode("utf-8")},
                reason=(
                    f"improver prose candidate {candidate.id} (generation {candidate.generation})"
                ),
            )
        except ValueError as exc:
            raise SkillRevisionRefusedError(str(exc)) from exc

        # Save candidate to store
        self._store.save(skill_name, candidate, active=True, frontier=True)

        # Append audit log
        improvement = {
            dim: candidate.aggregate_scores.get(dim, 0) - seed_scores.get(dim, 0)
            for dim in seed_scores
        }
        event = MutationEvent(
            timestamp=datetime.now(UTC),
            skill_name=skill_name,
            previous_hash=previous_hash,
            new_hash=candidate.fingerprint,
            candidate_id=candidate.id,
            generation=candidate.generation,
            scores=dict(candidate.aggregate_scores),
            improvement=improvement,
            stop_reason="applied",
            trace_ids=trace_ids,
        )
        self._store.append_audit(skill_name, event)
        _logger.info(
            "Applied optimization for %s: generation %d, improvement %s",
            skill_name,
            candidate.generation,
            improvement,
        )
        return revision
