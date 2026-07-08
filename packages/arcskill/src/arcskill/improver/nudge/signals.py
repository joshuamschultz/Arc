"""NudgeSignals — snapshot of the 4 trigger conditions for a single turn.

Captures boolean trigger flags and turn metadata derived from trace_collector
signals. Intentionally a plain dataclass (no business logic) — NudgeEmitter
owns all evaluation logic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NudgeSignals:
    """Four trigger conditions plus turn metadata for one agent:post_plan event.

    All boolean fields map 1:1 to conditions in the trigger conjunction
    defined in SDD §3.7. See NudgeEmitter._evaluate_trigger for the
    full conjunction evaluation.

    Fields
    ------
    tool_calls_ok:
        Number of tool calls with result_status == "ok" in the current
        turn. Derived from trace_collector active_span.tool_calls.
    task_outcome:
        Heuristic outcome set by trace_collector ("success" | "failure" |
        "partial"). NudgeEmitter only fires on "success".
    error_count:
        Number of tool calls with result_status == "error" in the current
        turn. Non-zero means error-recovery occurred.
    user_correction_detected:
        True if the session context contains a correction signal. Currently
        unused in v1 (always False) — reserved for future turn-level
        annotation.
    max_existing_skill_coverage:
        Highest coverage_pct / 100.0 across all known skills for this turn's
        tool sequence. < 0.3 means the turn's workflow is not well covered
        by any existing skill.
    turn_number:
        Turn number from trace_collector at time of evaluation.
    trace_id:
        Trace ID of the most-recently closed span (for audit cross-reference).
    """

    tool_calls_ok: int
    task_outcome: str
    error_count: int
    user_correction_detected: bool
    max_existing_skill_coverage: float
    turn_number: int
    trace_id: str = ""
