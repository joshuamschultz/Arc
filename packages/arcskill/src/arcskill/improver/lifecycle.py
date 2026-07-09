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
    ) -> None:
        self._store = store
        self._config = config
        self._load_traces = load_traces
        self._generation_of = generation_of

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
        """Operator-initiated restore from lineage → active (REQ-044)."""
        previous = self._store.set_lifecycle_state(skill_name, STATE_ACTIVE, reason="revived")
        _logger.info("skill %s revived by operator", skill_name)
        return LifecycleEvent(
            datetime.now(UTC), skill_name, previous, STATE_ACTIVE, "operator revive"
        )


__all__ = [
    "STATE_ACTIVE",
    "STATE_RETIRED",
    "STATE_UNDERPERFORMING",
    "SkillLifecycle",
    "UsageStats",
]
