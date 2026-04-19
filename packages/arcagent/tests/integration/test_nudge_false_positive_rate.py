"""Integration test: nudge false-positive rate <= 5% on synthetic conversations.

Gate G2.4 from PLAN §M2: Nudge false-positive rate < 5% on synthetic conversation suite.

A false positive is defined as: a nudge fires on a conversation that does NOT
exhibit a genuine multi-tool, error-recovery workflow — i.e., the trigger
conjunction should NOT fire.

Test strategy:
- Generate 100 synthetic "conversations" from fixtures representing common
  low-complexity turn patterns.
- For each, build an EventContext and call NudgeEmitter.on_post_plan().
- Count nudges emitted.
- Assert: false_positive_count / total <= 0.05

Fixture categories (covering ~95% of real agent turns):
1. Simple 1-3 tool turns (read/respond) — should NEVER fire
2. Successful 4-tool turns without errors — should NOT fire (< 5 tools)
3. 5+ tool success turns WITH existing skill coverage >= 0.3 — should NOT fire
4. Failed/partial turns — should NOT fire
5. Only 1 error but < 5 ok tools — should NOT fire

Genuine true-positive fixture (NOT counted as false positives):
6. 6+ tool success + error_count >= 1 + coverage < 0.3 — SHOULD fire

The test asserts <= 5 false positives across 100 conversations.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from arcagent.core.module_bus import EventContext
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.nudge.nudge_emitter import NudgeEmitter


# ---------------------------------------------------------------------------
# Fixture type
# ---------------------------------------------------------------------------

@dataclass
class ConversationFixture:
    """A synthetic conversation turn fixture."""

    name: str
    tool_calls_ok: int
    error_count: int
    task_outcome: str
    max_existing_skill_coverage: float
    user_correction_detected: bool
    expected_fires: bool  # True = legitimate nudge, False = false-positive if fires


# ---------------------------------------------------------------------------
# Fixture library
# ---------------------------------------------------------------------------

def _build_fixtures() -> list[ConversationFixture]:
    """Build 100 synthetic conversation fixtures.

    Distribution:
    - 30 simple 1-3 tool read/respond turns (definitely no nudge)
    - 20 4-tool success turns (under 5 threshold)
    - 20 5+ tool success turns with high coverage (no novelty)
    - 10 5+ tool failed/partial turns
    - 10 5+ tool success + error but high coverage (no nudge — coverage blocks)
    - 10 genuine true positives (5+ ok + error + low coverage)

    Only the last 10 are expected to fire.
    """
    fixtures: list[ConversationFixture] = []

    # Category 1: simple 1-3 tool turns (30 fixtures)
    for i in range(10):
        fixtures.append(ConversationFixture(
            name=f"simple-1-tool-{i}",
            tool_calls_ok=1,
            error_count=0,
            task_outcome="success",
            max_existing_skill_coverage=0.8,
            user_correction_detected=False,
            expected_fires=False,
        ))
    for i in range(10):
        fixtures.append(ConversationFixture(
            name=f"simple-2-tool-{i}",
            tool_calls_ok=2,
            error_count=0,
            task_outcome="success",
            max_existing_skill_coverage=0.6,
            user_correction_detected=False,
            expected_fires=False,
        ))
    for i in range(10):
        fixtures.append(ConversationFixture(
            name=f"simple-3-tool-{i}",
            tool_calls_ok=3,
            error_count=0,
            task_outcome="success",
            max_existing_skill_coverage=0.5,
            user_correction_detected=False,
            expected_fires=False,
        ))

    # Category 2: 4-tool success turns (20 fixtures) — below 5 threshold
    for i in range(20):
        fixtures.append(ConversationFixture(
            name=f"four-tools-{i}",
            tool_calls_ok=4,
            error_count=0,
            task_outcome="success",
            max_existing_skill_coverage=0.2,  # low coverage but NOT enough tools
            user_correction_detected=False,
            expected_fires=False,
        ))

    # Category 3: 5+ tool success with high coverage (20 fixtures) — no novelty
    for i in range(20):
        fixtures.append(ConversationFixture(
            name=f"high-coverage-{i}",
            tool_calls_ok=6,
            error_count=0,
            task_outcome="success",
            max_existing_skill_coverage=0.7,  # >= 0.3 and no errors
            user_correction_detected=False,
            expected_fires=False,
        ))

    # Category 4: failed/partial turns (10 fixtures)
    for i in range(5):
        fixtures.append(ConversationFixture(
            name=f"failure-{i}",
            tool_calls_ok=5,
            error_count=5,
            task_outcome="failure",
            max_existing_skill_coverage=0.1,
            user_correction_detected=False,
            expected_fires=False,
        ))
    for i in range(5):
        fixtures.append(ConversationFixture(
            name=f"partial-{i}",
            tool_calls_ok=5,
            error_count=2,
            task_outcome="partial",
            max_existing_skill_coverage=0.1,
            user_correction_detected=False,
            expected_fires=False,
        ))

    # Category 5: high-coverage with errors (but coverage >= 0.3) — 10 fixtures
    # Actually: error_count >= 1 is enough for novelty regardless of coverage
    # So these SHOULD fire unless we make coverage high enough to NOT matter
    # But wait: novelty = error_count>=1 OR user_correction OR coverage<0.3
    # error_count>=1 is one of the OR conditions -> this WOULD fire
    # We need to pick a category that genuinely should NOT fire
    # Category 5b: 5 ok tools, 0 errors, coverage=0.35 (above 0.3), no correction
    for i in range(10):
        fixtures.append(ConversationFixture(
            name=f"no-novelty-5tool-{i}",
            tool_calls_ok=5,
            error_count=0,
            task_outcome="success",
            max_existing_skill_coverage=0.35,  # > 0.3, no errors, no correction
            user_correction_detected=False,
            expected_fires=False,
        ))

    # Category 6: genuine true positives (10 fixtures)
    for i in range(10):
        fixtures.append(ConversationFixture(
            name=f"true-positive-{i}",
            tool_calls_ok=6,
            error_count=1,
            task_outcome="success",
            max_existing_skill_coverage=0.2,
            user_correction_detected=False,
            expected_fires=True,
        ))

    assert len(fixtures) == 100, f"Expected 100 fixtures, got {len(fixtures)}"
    return fixtures


# ---------------------------------------------------------------------------
# Helper: build EventContext from fixture
# ---------------------------------------------------------------------------

def _make_ctx_from_fixture(
    fixture: ConversationFixture,
    turn_number: int = 0,
) -> EventContext:
    """Build an EventContext from a conversation fixture."""
    n_ok = fixture.tool_calls_ok
    n_err = fixture.error_count
    tool_names = [f"tool_{i}" for i in range(n_ok)]

    tool_calls: list[dict[str, Any]] = [
        {"tool_name": name, "result_status": "ok", "duration_ms": 10.0}
        for name in tool_names
    ]
    for j in range(n_err):
        tool_calls.append({
            "tool_name": f"error_tool_{j}",
            "result_status": "error",
            "duration_ms": 5.0,
        })

    return EventContext(
        event="agent:post_plan",
        data={
            "tool_calls": tool_calls,
            "task_outcome": fixture.task_outcome,
            "user_correction_detected": fixture.user_correction_detected,
            "max_existing_skill_coverage": fixture.max_existing_skill_coverage,
            "turn_number": turn_number,
            "trace_id": f"trace-{fixture.name}",
            "tool_names": tool_names,
            "outcome_source": "heuristic",
        },
        agent_did="did:arc:test-agent",
        trace_id=f"trace-{fixture.name}",
    )


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def test_synthetic_conversation_false_positive_rate_below_5pct() -> None:
    """G2.4: Nudge false-positive rate < 5% on synthetic conversation suite.

    Runs 100 synthetic conversations, counts nudges on fixtures where
    expected_fires=False, and asserts false_positive_rate <= 5%.
    """
    fixtures = _build_fixtures()
    assert len(fixtures) == 100

    # Create a fresh emitter per session (no shared cooldown state)
    # Use trace_buffer_turns=0 and cooloff_turns=0 so cooldowns don't mask
    # false positives — we want pure trigger conjunction testing here.
    config = SkillImproverConfig(
        trace_buffer_turns=0,  # disable global cooldown for this test
        cooloff_turns=0,  # disable shape suppression
    )

    false_positive_count = 0
    total_negative_fixtures = sum(1 for f in fixtures if not f.expected_fires)

    for idx, fixture in enumerate(fixtures):
        # Each fixture gets its own emitter to avoid cross-fixture cooldown
        emitter = NudgeEmitter(
            config=config,
            session_id=f"test-session-{idx}",
        )

        ctx = _make_ctx_from_fixture(fixture, turn_number=idx * 1000)
        asyncio.get_event_loop().run_until_complete(emitter.on_post_plan(ctx))

        nudge_fired = emitter.session_nudge_count > 0

        if not fixture.expected_fires and nudge_fired:
            false_positive_count += 1

    false_positive_rate = false_positive_count / total_negative_fixtures
    assert false_positive_rate <= 0.05, (
        f"False positive rate {false_positive_rate:.1%} exceeds 5% threshold. "
        f"False positives: {false_positive_count}/{total_negative_fixtures}"
    )
