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


def test_sweep_retires_inactive_skill(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=45)  # older than the 30-day window
    lc, store = _lifecycle(tmp_path, {"stale": [_trace("stale", "success", old)]})
    events = lc.sweep()
    assert len(events) == 1
    assert events[0].to_state == STATE_RETIRED
    assert "inactive" in events[0].reason
    assert store.lifecycle_state("stale") == STATE_RETIRED


def test_sweep_keeps_recently_used_skill(tmp_path: Path) -> None:
    recent = datetime.now(UTC) - timedelta(days=2)
    lc, store = _lifecycle(tmp_path, {"fresh": [_trace("fresh", "success", recent)]})
    assert lc.sweep() == []
    assert store.lifecycle_state("fresh") == "active"


def test_sweep_retires_exhausted_underperformer(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    failing = [_trace("bad", "failure", now) for _ in range(6)]
    lc, store = _lifecycle(
        tmp_path, {"bad": failing}, gen=3,
        config=LifecycleConfig(min_uses_before_retire=5, improve_attempts_before_retire=3),
    )
    events = lc.sweep()
    assert len(events) == 1
    assert "success floor" in events[0].reason
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


# --- AC-5: facade sweep retires + operator revive restores, both audited ---------


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_ac5_sweep_retire_then_operator_revive_are_audited(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    # Persist a real trace via the improver's own store so list_skills discovers it.
    ws = tmp_path / "ws"
    traces_dir = ws / "skill_traces" / "old-skill"
    traces_dir.mkdir(parents=True)
    (traces_dir / "traces-2020-01.jsonl").write_text(
        json.dumps(_trace("old-skill", "success", old).to_dict(), default=str) + "\n",
        encoding="utf-8",
    )
    sink = _Sink()
    imp = ArcSkillImprover(ws, config=ImproverConfig(), tier="federal", audit_sink=sink)

    await imp.review_lifecycle(turn=1)
    retired = [e for e in sink.events if getattr(e, "action", "") == "skill.lifecycle.retired"]
    assert retired, "sweep should retire the inactive skill"
    assert getattr(retired[0], "tier", None) == "federal"

    imp.revive("old-skill")
    revived = [e for e in sink.events if getattr(e, "action", "") == "skill.lifecycle.revived"]
    assert revived, "operator revive should emit an audit event"
    assert getattr(revived[0], "tier", None) == "federal"
