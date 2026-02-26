"""Per-example Pareto frontier for skill candidate selection.

Implements multi-objective optimization where each trace is a separate
objective. Token count is an additional dimension (lower is better).
Candidates are non-dominated if no other candidate beats them on every
trace simultaneously.
"""

from __future__ import annotations

import random
from typing import Any

from arcagent.modules.skill_improver.models import Candidate


class ParetoFrontier:
    """Pareto frontier maintaining non-dominated skill candidates."""

    def __init__(self) -> None:
        self._candidates: list[Candidate] = []

    @property
    def candidates(self) -> list[Candidate]:
        """Current frontier candidates."""
        return list(self._candidates)

    def _score_vector(self, candidate: Candidate) -> list[float]:
        """Build a unified score vector: per-trace scores + inverted token ratio.

        For token count, we invert so that lower tokens = higher score,
        making it consistent with the "higher is better" convention.
        """
        scores: list[float] = []
        for dim_scores in candidate.scores.values():
            scores.extend(dim_scores)
        # Token count as inverted dimension (lower is better)
        # Normalize to roughly same scale (divide by 100 to bring into score range)
        scores.append(-candidate.token_count / 100.0)
        return scores

    def dominates(self, a: Candidate, b: Candidate) -> bool:
        """A dominates B iff A >= B on every dimension AND > on at least one."""
        a_scores = self._score_vector(a)
        b_scores = self._score_vector(b)

        # Pad shorter vector if dimensions don't match
        max_len = max(len(a_scores), len(b_scores))
        a_scores.extend([0.0] * (max_len - len(a_scores)))
        b_scores.extend([0.0] * (max_len - len(b_scores)))

        at_least_one_better = False
        for sa, sb in zip(a_scores, b_scores, strict=True):
            if sa < sb:
                return False
            if sa > sb:
                at_least_one_better = True
        return at_least_one_better

    def add(self, candidate: Candidate) -> bool:
        """Add candidate if not dominated. Evict any candidates it dominates."""
        for existing in self._candidates:
            if self.dominates(existing, candidate):
                return False
        self._candidates = [c for c in self._candidates if not self.dominates(candidate, c)]
        self._candidates.append(candidate)
        return True

    def add_if_improves(self, candidate: Candidate, min_delta: float) -> bool:
        """Add only if candidate exceeds parent by min_delta on at least one dimension."""
        if candidate.parent_id is None:
            return self.add(candidate)
        parent = self._find(candidate.parent_id)
        if parent is None:
            return self.add(candidate)
        for dim in candidate.aggregate_scores:
            child_score = candidate.aggregate_scores[dim]
            parent_score = parent.aggregate_scores.get(dim, 0.0)
            if child_score - parent_score >= min_delta:
                return self.add(candidate)
        return False

    def select(self) -> Candidate:
        """Random selection from frontier."""
        if not self._candidates:
            msg = "Cannot select from empty frontier"
            raise ValueError(msg)
        return random.choice(self._candidates)  # noqa: S311 — not cryptographic

    def overall_best(self) -> Candidate:
        """Candidate with highest average across all dimensions."""
        if not self._candidates:
            msg = "Cannot select best from empty frontier"
            raise ValueError(msg)
        return max(
            self._candidates,
            key=lambda c: (
                sum(c.aggregate_scores.values()) / len(c.aggregate_scores)
                if c.aggregate_scores
                else 0.0
            ),
        )

    def _find(self, candidate_id: str) -> Candidate | None:
        """Find a candidate by ID."""
        for c in self._candidates:
            if c.id == candidate_id:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize frontier for persistence."""
        return {
            "candidates": [c.to_dict() for c in self._candidates],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParetoFrontier:
        """Restore frontier from serialized data."""
        frontier = cls()
        for c_data in data.get("candidates", []):
            frontier._candidates.append(Candidate.from_dict(c_data))
        return frontier
