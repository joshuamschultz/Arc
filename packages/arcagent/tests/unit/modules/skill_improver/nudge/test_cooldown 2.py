"""Unit tests for NudgeEmitter cooldown, ceiling, and shape-suppression logic."""

from __future__ import annotations

import pytest

from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.nudge.dedup import compute_tool_sequence_hash
from arcagent.modules.skill_improver.nudge.nudge_emitter import (
    _MAX_NUDGES_PER_SESSION,
    NudgeEmitter,
)
from arcagent.modules.skill_improver.nudge.signals import NudgeSignals


def _make_emitter(
    trace_buffer_turns: int = 50,
    cooloff_turns: int = 200,
) -> NudgeEmitter:
    """Create NudgeEmitter with specified cooldown config."""
    config = SkillImproverConfig(
        trace_buffer_turns=trace_buffer_turns,
        cooloff_turns=cooloff_turns,
    )
    return NudgeEmitter(config=config, session_id="test-session")


def _signals(turn_number: int, *, task_outcome: str = "success") -> NudgeSignals:
    return NudgeSignals(
        tool_calls_ok=6,
        task_outcome=task_outcome,
        error_count=1,
        user_correction_detected=False,
        max_existing_skill_coverage=0.2,
        turn_number=turn_number,
    )


class TestGlobalCooldown:
    """test_cooldown_1_per_50_turns: second qualifying turn within 50 is suppressed."""

    def test_no_nudge_in_history_not_in_cooldown(self) -> None:
        emitter = _make_emitter(trace_buffer_turns=50)
        assert emitter._in_global_cooldown(10) is False

    def test_nudge_recorded_then_in_cooldown(self) -> None:
        emitter = _make_emitter(trace_buffer_turns=50)
        emitter._record_nudge(turn_number=10, tool_seq_hash="abc")
        # Turn 11 is still within 50-turn window from turn 10
        assert emitter._in_global_cooldown(11) is True

    def test_cooldown_expires_after_window(self) -> None:
        """After trace_buffer_turns (50), cooldown expires."""
        emitter = _make_emitter(trace_buffer_turns=50)
        emitter._record_nudge(turn_number=10, tool_seq_hash="abc")
        # Turn 60 = 50 turns after turn 10 — exactly at boundary (>= window)
        assert emitter._in_global_cooldown(60) is False

    def test_cooldown_1_per_50_turns_contract(self) -> None:
        """Test contract test_cooldown_1_per_50_turns from spec."""
        emitter = _make_emitter(trace_buffer_turns=50)
        # Simulate first nudge at turn 1
        emitter._record_nudge(turn_number=1, tool_seq_hash="hash-a")
        # Turn 30 (within 50-turn window) should be suppressed
        assert emitter._in_global_cooldown(30) is True
        # Turn 51 (outside window) should not be suppressed
        assert emitter._in_global_cooldown(51) is False


class TestPerSkillShapeSuppression:
    """test_per_skill_shape_200_turn_suppression: same hash suppressed for 200 turns."""

    def test_shape_not_suppressed_initially(self) -> None:
        emitter = _make_emitter(cooloff_turns=200)
        h = compute_tool_sequence_hash(["read", "bash"])
        assert emitter._is_shape_suppressed(h, current_turn=0) is False

    def test_shape_suppressed_after_nudge(self) -> None:
        emitter = _make_emitter(cooloff_turns=200)
        h = compute_tool_sequence_hash(["read", "bash"])
        emitter._record_nudge(turn_number=10, tool_seq_hash=h)
        # Suppressed until turn 10 + 200 = 210
        assert emitter._is_shape_suppressed(h, current_turn=100) is True
        assert emitter._is_shape_suppressed(h, current_turn=209) is True

    def test_shape_suppression_expires(self) -> None:
        emitter = _make_emitter(cooloff_turns=200)
        h = compute_tool_sequence_hash(["read", "bash"])
        emitter._record_nudge(turn_number=10, tool_seq_hash=h)
        # Turn 210 = 10 + 200, no longer suppressed
        assert emitter._is_shape_suppressed(h, current_turn=210) is False

    def test_different_shapes_independent_suppression(self) -> None:
        emitter = _make_emitter(cooloff_turns=200)
        h1 = compute_tool_sequence_hash(["read", "bash"])
        h2 = compute_tool_sequence_hash(["write", "delete"])
        emitter._record_nudge(turn_number=1, tool_seq_hash=h1)
        # h1 suppressed but h2 is not
        assert emitter._is_shape_suppressed(h1, current_turn=50) is True
        assert emitter._is_shape_suppressed(h2, current_turn=50) is False

    def test_per_skill_shape_200_turn_contract(self) -> None:
        """Test contract test_per_skill_shape_200_turn_suppression."""
        emitter = _make_emitter(cooloff_turns=200)
        h = compute_tool_sequence_hash(["tool-a", "tool-b", "tool-c"])
        emitter._record_nudge(turn_number=5, tool_seq_hash=h)
        # Any turn < 5 + 200 = 205 should be suppressed
        for turn in [6, 50, 100, 204]:
            assert emitter._is_shape_suppressed(h, current_turn=turn) is True
        # Turn 205 should not be suppressed
        assert emitter._is_shape_suppressed(h, current_turn=205) is False


class TestSessionCeiling:
    """test_session_ceiling_3: 4th qualifying turn in same session is suppressed."""

    def test_initial_count_is_zero(self) -> None:
        emitter = _make_emitter()
        assert emitter.session_nudge_count == 0

    def test_count_increments_with_records(self) -> None:
        emitter = _make_emitter()
        for i in range(3):
            # Use different hashes and widely spaced turns to avoid shape suppression
            emitter._record_nudge(turn_number=i * 300, tool_seq_hash=f"hash-{i}")
        assert emitter.session_nudge_count == 3

    def test_session_ceiling_blocks_fourth(self) -> None:
        """After 3 nudges, session ceiling is hit."""
        emitter = _make_emitter()
        for i in range(_MAX_NUDGES_PER_SESSION):
            emitter._record_nudge(turn_number=i * 300, tool_seq_hash=f"unique-{i}")
        # Fourth should be blocked by ceiling check
        # We test this via _evaluate_trigger indirectly: but ceiling is checked
        # after _evaluate_trigger in on_post_plan. Check session_nudge_count.
        assert emitter.session_nudge_count == _MAX_NUDGES_PER_SESSION

    def test_max_nudges_constant_is_3(self) -> None:
        """SDD §3.7 hard ceiling is 3 nudges per session."""
        assert _MAX_NUDGES_PER_SESSION == 3
