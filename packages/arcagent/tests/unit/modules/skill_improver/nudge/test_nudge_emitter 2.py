"""Unit tests for NudgeEmitter — all 12 test-contract cases from PLAN T2.5."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.module_bus import EventContext, ModuleBus
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.nudge.dedup import compute_tool_sequence_hash
from arcagent.modules.skill_improver.nudge.nudge_emitter import (
    EFFECTIVE_PRIORITY,
    SDD_STATED_PRIORITY,
    _MAX_NUDGES_PER_SESSION,
    NudgeEmitter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs: Any) -> SkillImproverConfig:
    return SkillImproverConfig(**kwargs)


def _make_emitter(
    config: SkillImproverConfig | None = None,
    telemetry: Any = None,
    session_id: str = "sess-test",
) -> NudgeEmitter:
    if config is None:
        config = _make_config()
    return NudgeEmitter(config=config, session_id=session_id, telemetry=telemetry)


def _make_ctx(
    *,
    tool_calls_ok: int = 6,
    error_count: int = 1,
    task_outcome: str = "success",
    user_correction_detected: bool = False,
    max_existing_skill_coverage: float = 0.2,
    turn_number: int = 10,
    trace_id: str = "trace-001",
    tool_names: list[str] | None = None,
    agent_did: str = "did:arc:agent1",
) -> EventContext:
    if tool_names is None:
        tool_names = ["read", "bash", "grep", "write", "search", "create"]

    # Build tool_calls list consistent with ok/error counts
    tool_calls = [
        {"tool_name": n, "result_status": "ok", "duration_ms": 10.0}
        for n in tool_names[:tool_calls_ok]
    ]
    for i in range(error_count):
        tool_calls.append(
            {"tool_name": f"failing_tool_{i}", "result_status": "error", "duration_ms": 5.0}
        )

    return EventContext(
        event="agent:post_plan",
        data={
            "tool_calls": tool_calls,
            "task_outcome": task_outcome,
            "user_correction_detected": user_correction_detected,
            "max_existing_skill_coverage": max_existing_skill_coverage,
            "turn_number": turn_number,
            "trace_id": trace_id,
            "tool_names": tool_names,
            "outcome_source": "heuristic",
        },
        agent_did=agent_did,
        trace_id=trace_id,
    )


class TestPriorityOrdering:
    """test_priority_150_after_trace_collector_200: verify ordering intent."""

    def test_sdd_stated_priority_is_150(self) -> None:
        """SDD §3.7 states priority 150 — constant must reflect spec."""
        assert SDD_STATED_PRIORITY == 150

    def test_effective_priority_runs_after_trace_collector(self) -> None:
        """Effective priority must be > 200 so trace_collector (200) runs first.

        module_bus: lower priority number = runs first.
        trace_collector is at 200; NudgeEmitter must be > 200 to run after.
        """
        assert EFFECTIVE_PRIORITY > 200

    def test_bus_subscription_uses_effective_priority(self) -> None:
        """startup() registers at EFFECTIVE_PRIORITY on the bus."""
        bus = ModuleBus()
        ctx = MagicMock()
        ctx.bus = bus

        emitter = _make_emitter()
        emitter.startup(ctx)

        regs = bus._handlers.get("agent:post_plan", [])
        assert len(regs) == 1
        assert regs[0].priority == EFFECTIVE_PRIORITY

    def test_priority_150_after_trace_collector_200_contract(self) -> None:
        """Test contract: NudgeEmitter subscription runs AFTER trace_collector.

        Verifies via bus priority: emitter priority > trace_collector priority (200).
        """
        assert EFFECTIVE_PRIORITY > 200, (
            f"NudgeEmitter priority {EFFECTIVE_PRIORITY} must be > "
            f"trace_collector priority 200 so trace_collector runs first"
        )


class TestTriggerConjunction:
    """Tests for the AND-conjunction defined in SDD §3.7."""

    def test_triggers_on_5_plus_tool_calls_with_error_recovery(self) -> None:
        """test_triggers_on_5_plus_tool_calls_with_error_recovery (contract test 2).

        6 ok tool calls + error_count=1 + task_outcome=success + coverage=0.2
        -> trigger evaluates True.
        """
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        signals = NudgeSignals(
            tool_calls_ok=6,
            task_outcome="success",
            error_count=1,
            user_correction_detected=False,
            max_existing_skill_coverage=0.2,
            turn_number=10,
        )
        assert emitter._evaluate_trigger(signals) is True

    def test_does_not_trigger_on_simple_3_turn_conversation(self) -> None:
        """test_does_not_trigger_on_simple_3_turn_conversation (contract test 3).

        3 tool calls, no errors, no correction -> does NOT trigger (false-positive guard).
        """
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        signals = NudgeSignals(
            tool_calls_ok=3,  # < 5
            task_outcome="success",
            error_count=0,
            user_correction_detected=False,
            max_existing_skill_coverage=0.5,
            turn_number=5,
        )
        assert emitter._evaluate_trigger(signals) is False

    def test_does_not_trigger_on_failure_outcome(self) -> None:
        """Non-success outcome blocks trigger even with many tool calls."""
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        signals = NudgeSignals(
            tool_calls_ok=10,
            task_outcome="failure",
            error_count=5,
            user_correction_detected=False,
            max_existing_skill_coverage=0.1,
            turn_number=5,
        )
        assert emitter._evaluate_trigger(signals) is False

    def test_does_not_trigger_when_no_novelty(self) -> None:
        """No error, no correction, high coverage -> no novelty -> no trigger."""
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        signals = NudgeSignals(
            tool_calls_ok=10,
            task_outcome="success",
            error_count=0,
            user_correction_detected=False,
            max_existing_skill_coverage=0.9,  # high coverage
            turn_number=5,
        )
        assert emitter._evaluate_trigger(signals) is False

    def test_triggers_via_user_correction(self) -> None:
        """user_correction_detected=True satisfies novelty condition."""
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        signals = NudgeSignals(
            tool_calls_ok=5,
            task_outcome="success",
            error_count=0,
            user_correction_detected=True,  # novelty via correction
            max_existing_skill_coverage=0.9,
            turn_number=5,
        )
        assert emitter._evaluate_trigger(signals) is True

    def test_triggers_via_low_coverage(self) -> None:
        """max_existing_skill_coverage < 0.3 satisfies novelty even without errors."""
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        signals = NudgeSignals(
            tool_calls_ok=5,
            task_outcome="success",
            error_count=0,
            user_correction_detected=False,
            max_existing_skill_coverage=0.1,  # < 0.3
            turn_number=5,
        )
        assert emitter._evaluate_trigger(signals) is True

    def test_does_not_trigger_when_in_cooldown(self) -> None:
        """Global cooldown blocks trigger."""
        emitter = _make_emitter()
        from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

        # Record a nudge at turn 5
        emitter._record_nudge(turn_number=5, tool_seq_hash="some-hash")

        signals = NudgeSignals(
            tool_calls_ok=6,
            task_outcome="success",
            error_count=1,
            user_correction_detected=False,
            max_existing_skill_coverage=0.1,
            turn_number=10,  # within 50-turn cooldown
        )
        assert emitter._evaluate_trigger(signals) is False


class TestExemptTagBlocking:
    """test_exempt_tag_blocks_nudge (contract test 10)."""

    def test_exempt_tag_blocks_nudge(self) -> None:
        """Skills with exempt tags must not trigger nudge."""
        emitter = _make_emitter()
        # "security-critical" is in default exempt_tags
        result = emitter.check_exempt_tags(["security-critical", "other"])
        assert result is True

    def test_non_exempt_tags_do_not_block(self) -> None:
        emitter = _make_emitter()
        result = emitter.check_exempt_tags(["general", "utility"])
        assert result is False

    def test_no_tags_do_not_block(self) -> None:
        emitter = _make_emitter()
        assert emitter.check_exempt_tags([]) is False

    def test_compliance_tag_is_exempt(self) -> None:
        emitter = _make_emitter()
        assert emitter.check_exempt_tags(["compliance"]) is True

    def test_auth_tag_is_exempt(self) -> None:
        emitter = _make_emitter()
        assert emitter.check_exempt_tags(["auth"]) is True


class TestDedup:
    """Dedup tests — contract tests 7, 8, 9."""

    async def test_dedup_name_collision_blocks_nudge(self) -> None:
        """test_dedup_name_collision: existing name -> no nudge + dedup_hit event."""
        telemetry = MagicMock()
        emitter = _make_emitter(telemetry=telemetry)

        # Inject known skill names that will collide with derived name
        # _derive_proposed_name(["read","bash","grep","write","search","run"]) -> "skill-bash-grep-read-run-search"
        emitter.update_known_skills(
            names={"skill-bash-grep-read-run-search"},
            fingerprints=set(),
            tool_lists=[],
        )

        ctx = _make_ctx(tool_names=["read", "bash", "grep", "write", "search", "run"])
        await emitter.on_post_plan(ctx)

        # Telemetry should have recorded a dedup hit, not a nudge_emitted
        event_names = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.nudge_dedup_hit" in event_names
        assert "skill_improver.nudge_emitted" not in event_names

    async def test_dedup_fingerprint_match(self) -> None:
        """test_dedup_fingerprint_match: SHA-256 match -> suppressed."""
        telemetry = MagicMock()
        emitter = _make_emitter(telemetry=telemetry)

        tool_names = ["read", "bash", "grep", "write", "search", "run"]
        seq_hash = compute_tool_sequence_hash(tool_names)
        emitter.update_known_skills(
            names=set(),
            fingerprints={seq_hash},
            tool_lists=[],
        )

        ctx = _make_ctx(tool_names=tool_names)
        await emitter.on_post_plan(ctx)

        event_names = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.nudge_dedup_hit" in event_names
        assert "skill_improver.nudge_emitted" not in event_names

    async def test_dedup_semantic_similarity_above_threshold(self) -> None:
        """test_dedup_semantic_similarity_above_threshold: cosine >= 0.85 -> suppressed."""
        telemetry = MagicMock()
        emitter = _make_emitter(telemetry=telemetry)

        tool_names = ["read", "bash", "grep", "write", "search", "run"]
        # Identical tool list in known_tool_lists -> cosine = 1.0
        emitter.update_known_skills(
            names=set(),
            fingerprints=set(),
            tool_lists=[tool_names],
        )

        ctx = _make_ctx(tool_names=tool_names)
        await emitter.on_post_plan(ctx)

        event_names = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.nudge_dedup_hit" in event_names
        assert "skill_improver.nudge_emitted" not in event_names


class TestTelemetryEmission:
    """test_nudge_emits_telemetry_event (contract test 11)."""

    async def test_nudge_emits_telemetry_event(self) -> None:
        """Successful nudge emits TelemetryEvent with full signal_vector."""
        telemetry = MagicMock()
        emitter = _make_emitter(telemetry=telemetry)

        ctx = _make_ctx(
            tool_calls_ok=6,
            error_count=1,
            task_outcome="success",
            max_existing_skill_coverage=0.2,
            turn_number=10,
            trace_id="trace-xyz",
        )
        await emitter.on_post_plan(ctx)

        # Should have called audit_event with "skill_improver.nudge_emitted"
        telemetry.audit_event.assert_called_once()
        call_args = telemetry.audit_event.call_args
        assert call_args.args[0] == "skill_improver.nudge_emitted"

        details = call_args.args[1]
        assert "turn_id" in details
        assert "session_id" in details
        assert "signal_vector" in details
        assert "tool_sequence_hash" in details
        assert "outcome_source" in details

        sv = details["signal_vector"]
        assert sv["tool_calls_ok"] == 6
        assert sv["task_outcome"] == "success"
        assert sv["error_count"] == 1

    async def test_session_nudge_count_increments(self) -> None:
        """Successful nudge increments session_nudge_count."""
        emitter = _make_emitter()
        ctx = _make_ctx()
        await emitter.on_post_plan(ctx)
        assert emitter.session_nudge_count == 1


class TestSessionCeiling:
    """test_session_ceiling_3 (contract test 6)."""

    async def test_session_ceiling_3(self) -> None:
        """4th qualifying turn in same session is suppressed."""
        telemetry = MagicMock()
        # Use large cooloff/buffer to avoid those suppressing our test
        config = _make_config(trace_buffer_turns=0, cooloff_turns=0)
        emitter = _make_emitter(config=config, telemetry=telemetry)

        # Force 3 nudges already counted
        emitter._session_nudge_count = _MAX_NUDGES_PER_SESSION  # == 3

        ctx = _make_ctx(tool_names=["toolA", "toolB", "toolC", "toolD", "toolE", "toolF"])
        await emitter.on_post_plan(ctx)

        # Should NOT have emitted because ceiling is hit
        event_names = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.nudge_emitted" not in event_names


class TestCooldownIntegration:
    """test_cooldown_1_per_50_turns (contract test 4)."""

    async def test_cooldown_1_per_50_turns(self) -> None:
        """Second qualifying turn within 50 is suppressed."""
        telemetry = MagicMock()
        emitter = _make_emitter(telemetry=telemetry)

        # First nudge at turn 10
        ctx1 = _make_ctx(turn_number=10)
        await emitter.on_post_plan(ctx1)
        assert emitter.session_nudge_count == 1

        # Second qualifying turn at turn 30 (within 50-turn window)
        telemetry.reset_mock()
        ctx2 = _make_ctx(
            turn_number=30,
            tool_names=["alpha", "beta", "gamma", "delta", "epsilon", "zeta"],
        )
        await emitter.on_post_plan(ctx2)

        # No additional nudge emitted
        event_names = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.nudge_emitted" not in event_names
        assert emitter.session_nudge_count == 1  # unchanged


class TestDeriveProposedName:
    """Unit test for _derive_proposed_name helper."""

    def test_basic_derivation(self) -> None:
        name = NudgeEmitter._derive_proposed_name(["read", "bash", "grep"])
        assert name == "skill-bash-grep-read"

    def test_empty_tools(self) -> None:
        name = NudgeEmitter._derive_proposed_name([])
        assert name == "skill-unnamed"

    def test_deduplicates_tools(self) -> None:
        name = NudgeEmitter._derive_proposed_name(["read", "read", "bash"])
        assert name == "skill-bash-read"

    def test_max_length(self) -> None:
        long_tools = [f"very-long-tool-name-{i}" for i in range(20)]
        name = NudgeEmitter._derive_proposed_name(long_tools)
        assert len(name) <= 100
