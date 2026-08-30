"""Skill lifecycle state machine + Curator usage-sweep (SPEC-044 REQ-041..045).

Drives ``active → underperforming → retired`` (and operator-initiated ``retired →
active`` revive) from accrued usage stats. Retire is **reversible**: disable + retain
lineage, never a destructive delete (D-8). Every transition is an audited
:class:`~arcskill.improver.models.LifecycleEvent` (operator-signed on the WORM chain —
Phase 7 pins the operator key).

Josh-locked: inactivity window default 30 days; all sweep settings live in
``config.toml``; retire = disable + lineage; revive is operator-initiated.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.config import LifecycleConfig
from arcskill.improver.models import LifecycleEvent, SkillTrace

_logger = logging.getLogger("arcskill.improver.lifecycle")

STATE_ACTIVE = "active"
STATE_UNDERPERFORMING = "underperforming"
STATE_RETIRED = "retired"
STATE_MERGED = "merged"

# Two skills whose SKILL.md bodies are at least this similar (difflib ratio) are
# flagged as consolidation candidates. Deliberately conservative — a false positive
# only costs one wasted (and gated) merge proposal; the sweep never applies anything
# on its own (D-10).
_DEFAULT_SIMILARITY_THRESHOLD = 0.72


@dataclass(frozen=True)
class ConsolidationCandidate:
    """One proposed merge: ``skill_b`` folds into ``skill_a`` (SPEC-044 Curator).

    Pure output of :meth:`SkillLifecycle.consolidation_candidates` — nothing is
    proposed, gated, or applied until the caller (``ArcSkillImprover.review_consolidation``)
    drives it through the Merger seam + EvalGate + approval ladder.
    """

    skill_a: str
    skill_b: str
    similarity: float
    reason: str


@dataclass(frozen=True)
class UsageStats:
    """Accrued usage of a skill across its traces (REQ-041)."""

    total: int
    success: int
    failure: int
    partial: int
    last_used: datetime | None

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 0.0


class SkillLifecycle:
    """Curator: grade skills from usage, retire the dead/failing, revive on operator ask."""

    def __init__(
        self,
        store: CandidateStore,
        config: LifecycleConfig,
        *,
        load_traces: Callable[[str], list[SkillTrace]],
        generation_of: Callable[[str], int],
        text_of: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._load_traces = load_traces
        self._generation_of = generation_of
        # Optional (REQ-consolidate): resolves a skill's current SKILL.md text so the
        # sweep can flag overlapping content. ``None`` (a BYO caller predating this)
        # degrades consolidation_candidates() to an empty list — never an error.
        self._text_of = text_of

    def state(self, skill_name: str) -> str:
        return self._store.lifecycle_state(skill_name)

    def usage_stats(self, traces: list[SkillTrace]) -> UsageStats:
        """Compute success/failure/partial counts + last-used timestamp from traces."""
        success = sum(1 for t in traces if t.task_outcome == "success")
        failure = sum(1 for t in traces if t.task_outcome == "failure")
        partial = sum(1 for t in traces if t.task_outcome == "partial")
        ended = [t.ended_at for t in traces if t.ended_at is not None]
        return UsageStats(len(traces), success, failure, partial, max(ended) if ended else None)

    def pending_retirements(self, *, now: datetime | None = None) -> list[tuple[str, str]]:
        """Grade every skill; return ``(skill_name, reason)`` for each that should retire.

        Pure — commits nothing. The caller (``ArcSkillImprover.review_lifecycle``) gates
        each proposed retirement through the tier approval ladder (federal requires
        operator approval, D-10) before committing it via :meth:`retire` (REQ-043).
        """
        moment = now or datetime.now(UTC)
        pending: list[tuple[str, str]] = []
        for skill_name in self._store.list_skills():
            if self.state(skill_name) == STATE_RETIRED:
                continue
            reason = self._retire_reason(skill_name, moment)
            if reason is not None:
                pending.append((skill_name, reason))
        return pending

    def _retire_reason(self, skill_name: str, now: datetime) -> str | None:
        """The reason a skill should retire (inactive / exhausted), or ``None`` to keep."""
        stats = self.usage_stats(self._load_traces(skill_name))
        if self._is_inactive(stats, now):
            return "inactive past window"
        if self._is_exhausted_underperformer(skill_name, stats):
            return "below success floor after retry budget"
        return None

    def _is_inactive(self, stats: UsageStats, now: datetime) -> bool:
        if stats.last_used is None:
            return False
        idle_days = (now - stats.last_used).total_seconds() / 86400.0
        return idle_days > self._config.inactivity_window_days

    def _is_exhausted_underperformer(self, skill_name: str, stats: UsageStats) -> bool:
        return (
            stats.total >= self._config.min_uses_before_retire
            and stats.success_rate < self._config.failure_floor
            and self._generation_of(skill_name) >= self._config.improve_attempts_before_retire
        )

    def retire(self, skill_name: str, *, reason: str) -> LifecycleEvent:
        """Disable + retain lineage (reversible); emit a transition event."""
        previous = self._store.set_lifecycle_state(skill_name, STATE_RETIRED, reason=reason)
        _logger.info("skill %s retired: %s", skill_name, reason)
        return LifecycleEvent(datetime.now(UTC), skill_name, previous, STATE_RETIRED, reason)

    def revive(self, skill_name: str) -> LifecycleEvent:
        """Operator-initiated restore from lineage → active (REQ-044).

        Un-merges the same way it un-retires: :meth:`consolidation_candidates` and
        :meth:`merge` never touch ``prior_active_id``/``active_candidate_id`` — restoring
        the pre-merge state is exactly the retire/revive machinery, reused.
        """
        previous = self._store.set_lifecycle_state(skill_name, STATE_ACTIVE, reason="revived")
        _logger.info("skill %s revived by operator", skill_name)
        return LifecycleEvent(
            datetime.now(UTC), skill_name, previous, STATE_ACTIVE, "operator revive"
        )

    def consolidation_candidates(
        self, *, threshold: float = _DEFAULT_SIMILARITY_THRESHOLD
    ) -> list[ConsolidationCandidate]:
        """Flag pairs of active skills whose bodies overlap enough to merit a merge.

        Pure — identifies, proposes nothing, commits nothing (mirrors
        :meth:`pending_retirements`). Deterministic text-similarity signal (no LLM call):
        the improver only spends a model call once a pair clears this bar. Retired and
        already-merged skills are excluded on both sides — they are not live duplicates.
        """
        if self._text_of is None:
            return []
        names = [n for n in self._store.list_skills() if self.state(n) == STATE_ACTIVE]
        texts = {n: self._text_of(n) for n in names}
        candidates: list[ConsolidationCandidate] = []
        for i, a in enumerate(names):
            text_a = texts[a]
            if not text_a:
                continue
            for b in names[i + 1 :]:
                text_b = texts[b]
                if not text_b:
                    continue
                ratio = difflib.SequenceMatcher(None, text_a, text_b).ratio()
                if ratio >= threshold:
                    candidates.append(
                        ConsolidationCandidate(
                            skill_a=a,
                            skill_b=b,
                            similarity=ratio,
                            reason=f"body similarity {ratio:.2f} >= threshold {threshold:.2f}",
                        )
                    )
        return candidates

    def merge(self, skill_name: str, *, into: str, reason: str) -> LifecycleEvent:
        """Non-destructive consolidation (D-8): mark ``skill_name`` merged into ``into``.

        Reversible exactly like retire — the candidate/lineage is retained and
        :meth:`revive` restores ``skill_name`` to ``active`` (its own prior text,
        untouched). Never a delete; the caller is responsible for the survivor's
        applied text (a normal candidate mutation on ``into``, gated the usual way).
        """
        previous = self._store.set_lifecycle_state(skill_name, STATE_MERGED, reason=reason)
        self._store.set_merge_target(skill_name, into)
        _logger.info("skill %s merged into %s: %s", skill_name, into, reason)
        return LifecycleEvent(datetime.now(UTC), skill_name, previous, STATE_MERGED, reason)


__all__ = [
    "STATE_ACTIVE",
    "STATE_MERGED",
    "STATE_RETIRED",
    "STATE_UNDERPERFORMING",
    "ConsolidationCandidate",
    "SkillLifecycle",
    "UsageStats",
]
