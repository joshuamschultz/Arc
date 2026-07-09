"""SPEC-044 AC-5 (CRITICAL-1) — the Curator sweep is driven by the REAL proactive engine.

Producers-unwired defense for the lifecycle sweep: an inactive skill is retired only when
the proactive-engine tick fires the registered schedule → the handler → the adapter's
``review_lifecycle`` → retire → operator-signed WORM audit. No direct ``review_lifecycle()``
facade call — the tick is the producer, exactly as it fires in production.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arctrust import OperatorKey, verify_chain

from arcagent.modules.proactive.engine import Schedule
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import skills_ready, skills_shutdown


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _seed_inactive_skill(ws: Path) -> None:
    old = datetime.now(UTC) - timedelta(days=60)  # older than the 30-day window
    traces_dir = ws / "skill_traces" / "old-skill"
    traces_dir.mkdir(parents=True)
    trace = {
        "trace_id": "t", "session_id": "s", "skill_name": "old-skill", "skill_version": 0,
        "turn_number": 0, "started_at": old.isoformat(), "ended_at": old.isoformat(),
        "tool_calls": [], "task_outcome": "success",
    }
    (traces_dir / "traces-2020-01.jsonl").write_text(
        json.dumps(trace) + "\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_ac5_proactive_tick_retires_inactive_skill_with_operator_audit(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "agent"
    ws.mkdir()
    _seed_inactive_skill(ws)
    operator = OperatorKey.generate()

    # Configure the skills module with the real improver (personal tier → retire auto-approved)
    # and an operator-signed WORM sink built from the operator key.
    _runtime.configure(
        config={"adapter": "arcskill", "tier": "personal"},
        workspace=ws,
        operator_signer=operator.into_signer(),
    )
    assert _runtime.state().active is True

    # agent:ready starts the sweep engine (the production producer wiring).
    await skills_ready(_Ctx(skill_registry=None))
    engine = _runtime.state().sweep_engine
    assert engine is not None, "agent:ready must start the lifecycle-sweep engine"

    # Halt the auto tick-loop for determinism, then force the schedule due and tick once —
    # this is exactly the dispatch the loop performs when the interval elapses.
    engine.stop()
    engine.add(
        Schedule(
            id=_runtime._SWEEP_SCHEDULE_ID,
            interval_seconds=1.0,
            next_run_monotonic=0.0,  # due now
            kind="heartbeat",
        )
    )
    await engine.tick()
    await engine.drain()  # await the fire-and-forget sweep handler task

    # The sweep retired the inactive skill — proven through the tick, not a facade call.
    assert _runtime.state().adapter.retired_skills() == frozenset({"old-skill"})
    # And it is now hidden from the agent's offering.
    assert "old-skill" in _runtime.retired_skill_names()

    # The retire transition landed on the operator-signed WORM chain.
    chain = ws.parent / ".audit" / "skills.worm"
    assert chain.exists(), "retire must emit an operator-signed WORM audit event"
    assert verify_chain(chain, operator.public_key) is True

    await skills_shutdown(_Ctx())
    assert _runtime.state().sweep_engine is None  # torn down on shutdown
