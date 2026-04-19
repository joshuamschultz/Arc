"""Unit tests for NudgeSignals dataclass."""

from __future__ import annotations

import pytest

from arcagent.modules.skill_improver.nudge.signals import NudgeSignals


class TestNudgeSignals:
    """NudgeSignals is a frozen dataclass — test construction and field access."""

    def test_default_trace_id_empty(self) -> None:
        """trace_id defaults to empty string when not supplied."""
        s = NudgeSignals(
            tool_calls_ok=5,
            task_outcome="success",
            error_count=1,
            user_correction_detected=False,
            max_existing_skill_coverage=0.2,
            turn_number=10,
        )
        assert s.trace_id == ""

    def test_all_fields_set(self) -> None:
        """All fields are accessible after construction."""
        s = NudgeSignals(
            tool_calls_ok=6,
            task_outcome="success",
            error_count=2,
            user_correction_detected=True,
            max_existing_skill_coverage=0.15,
            turn_number=42,
            trace_id="abc-123",
        )
        assert s.tool_calls_ok == 6
        assert s.task_outcome == "success"
        assert s.error_count == 2
        assert s.user_correction_detected is True
        assert s.max_existing_skill_coverage == pytest.approx(0.15)
        assert s.turn_number == 42
        assert s.trace_id == "abc-123"

    def test_frozen_immutability(self) -> None:
        """NudgeSignals is frozen — mutation raises."""
        s = NudgeSignals(
            tool_calls_ok=5,
            task_outcome="success",
            error_count=0,
            user_correction_detected=False,
            max_existing_skill_coverage=0.5,
            turn_number=1,
        )
        with pytest.raises((AttributeError, TypeError)):
            s.tool_calls_ok = 99  # type: ignore[misc]

    def test_failure_outcome(self) -> None:
        """task_outcome can hold any string (NudgeEmitter checks for 'success')."""
        s = NudgeSignals(
            tool_calls_ok=3,
            task_outcome="failure",
            error_count=3,
            user_correction_detected=False,
            max_existing_skill_coverage=0.0,
            turn_number=5,
        )
        assert s.task_outcome == "failure"

    def test_zero_error_count(self) -> None:
        """Zero error_count is valid — nudge can still fire via low coverage."""
        s = NudgeSignals(
            tool_calls_ok=7,
            task_outcome="success",
            error_count=0,
            user_correction_detected=False,
            max_existing_skill_coverage=0.1,
            turn_number=20,
        )
        assert s.error_count == 0
        assert s.max_existing_skill_coverage == pytest.approx(0.1)
