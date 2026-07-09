"""SPEC-044 Phase 6 — lifecycle state machine + Curator sweep + AC-5 (REQ-041..045).

Retire is reversible (disable + retain lineage); revive is operator-initiated; every
transition emits a tier-stamped audit event on the WORM sink.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.config import LifecycleConfig
from arcskill.improver.lifecycle import STATE_RETIRED, SkillLifecycle
from arcskill.improver.models import SkillTrace


def _trace(skill: str, outcome: str, ended: datetime) -> SkillTrace:
    return SkillTrace(
        trace_id="t", session_id="s", skill_name=skill, skill_version=0, turn_number=0,
        started_at=ended, ended_at=ended, task_outcome=outcome,
    )


def _lifecycle(tmp_path: Path, traces: dict[str, list[SkillTrace]], *, gen: int = 0,
               config: LifecycleConfig | None = None) -> tuple[SkillLifecycle, CandidateStore]:
    store = CandidateStore(tmp_path)
    # Seed a candidate dir per skill so list_skills() discovers it.
    for name in traces:
        (tmp_path / "skill_traces" / name).mkdir(parents=True, exist_ok=True)
    lc = SkillLifecycle(
        store, config or LifecycleConfig(),
        load_traces=lambda n: traces.get(n, []),
        generation_of=lambda n: gen,
    )
    return lc, store


def test_usage_stats_success_rate(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    lc, _ = _lifecycle(tmp_path, {})
    stats = lc.usage_stats(
        [_trace("s", "success", now), _trace("s", "failure", now), _trace("s", "success", now)]
    )
    assert stats.total == 3
    assert stats.success == 2
    assert abs(stats.success_rate - 2 / 3) < 1e-9


def test_pending_retirements_flags_inactive_skill(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=45)  # older than the 30-day window
    lc, store = _lifecycle(tmp_path, {"stale": [_trace("stale", "success", old)]})
    pending = lc.pending_retirements()
    assert len(pending) == 1
    name, reason = pending[0]
    assert name == "stale"
    assert "inactive" in reason
    # pending_retirements is pure — nothing committed until the caller retires.
    assert store.lifecycle_state("stale") == "active"
    event = lc.retire(name, reason=reason)
    assert event.to_state == STATE_RETIRED
    assert store.lifecycle_state("stale") == STATE_RETIRED


def test_pending_retirements_keeps_recently_used_skill(tmp_path: Path) -> None:
    recent = datetime.now(UTC) - timedelta(days=2)
    lc, store = _lifecycle(tmp_path, {"fresh": [_trace("fresh", "success", recent)]})
    assert lc.pending_retirements() == []
    assert store.lifecycle_state("fresh") == "active"


def test_pending_retirements_flags_exhausted_underperformer(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    failing = [_trace("bad", "failure", now) for _ in range(6)]
    lc, store = _lifecycle(
        tmp_path, {"bad": failing}, gen=3,
        config=LifecycleConfig(min_uses_before_retire=5, improve_attempts_before_retire=3),
    )
    pending = lc.pending_retirements()
    assert len(pending) == 1
    name, reason = pending[0]
    assert "success floor" in reason
    lc.retire(name, reason=reason)
    assert store.lifecycle_state("bad") == STATE_RETIRED


def test_revive_restores_from_retired(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    lc, store = _lifecycle(tmp_path, {"x": [_trace("x", "success", now)]})
    lc.retire("x", reason="test")
    assert store.lifecycle_state("x") == STATE_RETIRED
    event = lc.revive("x")
    assert event.to_state == "active"
    assert event.from_state == STATE_RETIRED
    assert store.lifecycle_state("x") == "active"


# --- improver lifecycle + approval gate (federal): retire/revive gated + audited --------
# The true AC-5 E2E (proactive tick → sweep → retire) lives in the arcagent extension test
# (drives the real producer); this exercises the improver's own gated-transition mechanics.


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


class _AutoApprover:
    """Callable ApprovalProvider that always grants — the wired-and-approved federal path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, action: str, skill_name: str, detail: str) -> bool:
        self.calls.append((action, skill_name))
        return True


def _seed_inactive_skill(ws: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    traces_dir = ws / "skill_traces" / "old-skill"
    traces_dir.mkdir(parents=True)
    (traces_dir / "traces-2020-01.jsonl").write_text(
        json.dumps(_trace("old-skill", "success", old).to_dict(), default=str) + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_federal_sweep_retire_then_revive_gated_and_audited(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_inactive_skill(ws)
    sink = _Sink()
    approver = _AutoApprover()
    imp = ArcSkillImprover(
        ws, config=ImproverConfig(), tier="federal", audit_sink=sink, approval_provider=approver
    )

    await imp.review_lifecycle(turn=1)
    retired = [e for e in sink.events if getattr(e, "action", "") == "skill.lifecycle.retired"]
    assert retired, "sweep should retire the inactive skill once approved"
    assert getattr(retired[0], "tier", None) == "federal"
    assert imp._candidate_store.lifecycle_state("old-skill") == STATE_RETIRED

    # HIGH-3: a retired skill is reported as retired (excluded from the agent offering).
    assert imp.retired_skills() == frozenset({"old-skill"})

    await imp.revive("old-skill")
    revived = [e for e in sink.events if getattr(e, "action", "") == "skill.lifecycle.revived"]
    assert revived, "approved operator revive should emit an audit event"
    # Both transitions passed through the operator-approval seam (federal, D-10).
    assert ("skill.lifecycle.retire", "old-skill") in approver.calls
    assert ("skill.lifecycle.revive", "old-skill") in approver.calls
    # HIGH-3: revive un-hides the skill — it is offered again.
    assert imp.retired_skills() == frozenset()


@pytest.mark.asyncio
async def test_federal_retire_blocked_without_approver(tmp_path: Path) -> None:
    """Federal retire fails closed when no approver is wired: state stays active, audited."""
    ws = tmp_path / "ws"
    _seed_inactive_skill(ws)
    sink = _Sink()
    imp = ArcSkillImprover(ws, config=ImproverConfig(), tier="federal", audit_sink=sink)

    await imp.review_lifecycle(turn=1)

    assert imp._candidate_store.lifecycle_state("old-skill") == "active"  # NOT retired
    retired = [e for e in sink.events if getattr(e, "action", "") == "skill.lifecycle.retired"]
    assert not retired
    denied = [e for e in sink.events if getattr(e, "outcome", "") == "denied_no_approver"]
    assert denied, "the blocked retirement must be audited"
