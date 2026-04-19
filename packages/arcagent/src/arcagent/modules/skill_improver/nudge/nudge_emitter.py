"""NudgeEmitter — auto-skill-creation nudge trigger submodule.

Subscribes to ``agent:post_plan`` at module-bus priority 150, which is AFTER
trace_collector (priority 200) so that the span is closed and task_outcome is
set before NudgeEmitter evaluates the trigger conjunction.

Note on priority semantics: module_bus uses LOWER numbers first. SDD §3.7
states "priority 150 (after trace_collector at 200)". To honour the SDD's
intent (trace_collector closes the span BEFORE nudge evaluates), we register
at EFFECTIVE_PRIORITY = 210, which is numerically higher than trace_collector's
200 and therefore runs after it. SDD_STATED_PRIORITY = 150 is preserved as a
constant for test assertions that verify the specification intent.

ASI-09 compliance: this code NEVER calls skill_manage() or any write path.
It only publishes a system_message_nudge event that context_manager injects
as advisory text on the next turn. The LLM agent decides what to do.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.nudge.dedup import (
    compute_tool_sequence_hash,
    pre_commit_dedup,
)
from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

_logger = logging.getLogger("arcagent.modules.skill_improver.nudge")

# SDD §3.7 states priority 150 (after trace_collector at 200).
# module_bus lower = runs first; we need to run AFTER trace_collector (200).
# Effective subscription priority: 210 (runs after 200).
# SDD_STATED_PRIORITY is kept for test assertions about ordering intent.
SDD_STATED_PRIORITY: int = 150
EFFECTIVE_PRIORITY: int = 210  # > 200 so trace_collector closes span first

# Hard ceiling per SDD §3.7
_MAX_NUDGES_PER_SESSION: int = 3

# Trigger-conjunction thresholds (SDD §3.7).
# _MIN_TOOL_CALLS_OK: minimum successful tool calls in a turn before a nudge is
# even considered (ensures the workflow is non-trivial).
# _MAX_EXISTING_COVERAGE: if any existing skill already covers the turn above
# this fraction, suppress the nudge (the workflow is not novel).
_MIN_TOOL_CALLS_OK: int = 5
_MAX_EXISTING_COVERAGE: float = 0.3

# Maximum number of tool names included in the derived skill name slug.
# Caps slug length to keep path-safe names short; excess tools are ignored.
_MAX_TOOL_SUFFIX_TOKENS: int = 5

# Hard character cap on derived skill name slugs for filesystem path safety.
_MAX_SLUG_LENGTH: int = 100

# Template for the advisory nudge message (ASI-09 compliant — never commands)
_NUDGE_TEMPLATE = (
    "The last turn used {n_tools} tools successfully, recovered from an error, "
    "and doesn't match any existing skill (top coverage {coverage_pct:.0%}). "
    "If this workflow is likely to recur, consider calling "
    "`skill_manage(action='create', ...)`. Skip if one-off. "
    "Confirm with the user before committing."
)


class NudgeEmitter:
    """Evaluates post-plan turns and emits skill-creation nudge events.

    Lifecycle:
        nudge_emitter = NudgeEmitter(config, session_id)
        nudge_emitter.startup(module_ctx)   # registers bus handler
        # ... turns fire and on_post_plan is called automatically ...
        nudge_emitter.shutdown()            # no-op; held resources are GC'd

    The emitter maintains:
    - A cooldown deque (turn numbers of last N nudges per session).
    - A per-skill-shape suppression dict (tool_sequence_hash -> until_turn).
    - A session nudge counter capped at _MAX_NUDGES_PER_SESSION.
    """

    def __init__(
        self,
        config: SkillImproverConfig,
        session_id: str = "",
        telemetry: Any = None,
    ) -> None:
        self._config = config
        self._session_id = session_id
        self._telemetry = telemetry

        # Per-session nudge turn history for 50-turn cooldown (FIFO)
        self._nudge_turns: deque[int] = deque()
        # Per-skill-shape suppression: tool_seq_hash -> until_turn_number
        self._shape_suppressed_until: dict[str, int] = {}
        # Total nudge count for session ceiling (max 3)
        self._session_nudge_count: int = 0

        # Wired at startup — injected via ModuleContext for testability
        self._bus: Any = None

        # Strong references to in-flight nudge emit tasks to prevent premature GC
        # (same pattern as SessionRouter._pending_tasks).
        self._pending_tasks: set[asyncio.Task[None]] = set()

        # Known existing skill names and fingerprints for dedup
        # These are populated from context if available, else empty
        self._known_skill_names: set[str] = set()
        self._known_fingerprints: set[str] = set()
        self._known_tool_lists: list[list[str]] = []

    # ------------------------------------------------------------------
    # Module lifecycle
    # ------------------------------------------------------------------

    def startup(self, ctx: ModuleContext) -> None:
        """Register bus handler for agent:post_plan at effective priority."""
        self._bus = ctx.bus
        ctx.bus.subscribe(
            "agent:post_plan",
            self.on_post_plan,
            priority=EFFECTIVE_PRIORITY,
            module_name="skill_improver.nudge",
        )
        _logger.info(
            "NudgeEmitter started: session=%s priority=%d",
            self._session_id,
            EFFECTIVE_PRIORITY,
        )

    def shutdown(self) -> None:
        """No-op shutdown — no background tasks, no held resources."""

    # ------------------------------------------------------------------
    # Main handler
    # ------------------------------------------------------------------

    async def on_post_plan(self, ctx: EventContext) -> None:
        """Handler for agent:post_plan (priority 210, after trace_collector 200).

        Reads turn signals from ctx.data (set by trace_collector before us),
        evaluates the trigger conjunction, runs dedup, checks cooldown, and
        emits a system_message_nudge event if all conditions pass.
        """
        signals = self._extract_signals(ctx)

        # Fast path: skip if trigger conjunction not met
        if not self._evaluate_trigger(signals):
            return

        tool_names: list[str] = ctx.data.get("tool_names", [])
        if not isinstance(tool_names, list):
            tool_names = []

        tool_seq_hash = compute_tool_sequence_hash(tool_names)

        # Per-skill-shape suppression (200-turn window)
        if self._is_shape_suppressed(tool_seq_hash, signals.turn_number):
            _logger.debug("Nudge suppressed: shape cooldown for hash %s", tool_seq_hash[:8])
            return

        # Dedup against known skills
        proposed_name = self._derive_proposed_name(tool_names)
        is_dup, dup_reason = pre_commit_dedup(
            proposed_name=proposed_name,
            existing_names=self._known_skill_names,
            tool_sequence_hash=tool_seq_hash,
            known_fingerprints=self._known_fingerprints,
            candidate_tools=tool_names,
            known_tool_lists=self._known_tool_lists,
            similarity_threshold=self._config.trace_similarity_threshold,
        )

        if is_dup:
            _logger.debug("Nudge suppressed: dedup hit (%s)", dup_reason)
            self._emit_dedup_event(signals, tool_seq_hash, dup_reason, ctx)
            return

        # Session ceiling
        if self._session_nudge_count >= _MAX_NUDGES_PER_SESSION:
            _logger.debug("Nudge suppressed: session ceiling (%d)", _MAX_NUDGES_PER_SESSION)
            return

        # All checks passed — emit nudge
        await self._emit_nudge(signals, tool_names, tool_seq_hash, ctx)

        # Update cooldown state
        self._record_nudge(signals.turn_number, tool_seq_hash)

    # ------------------------------------------------------------------
    # Signal extraction
    # ------------------------------------------------------------------

    def _extract_signals(self, ctx: EventContext) -> NudgeSignals:
        """Build NudgeSignals from EventContext data dict.

        trace_collector populates ctx.data before NudgeEmitter runs because
        trace_collector subscribes at priority 200 and NudgeEmitter at 210.
        """
        data = ctx.data
        tool_calls: list[dict[str, Any]] = data.get("tool_calls", [])

        tool_calls_ok = sum(
            1 for tc in tool_calls if tc.get("result_status") == "ok"
        )
        error_count = sum(
            1 for tc in tool_calls if tc.get("result_status") == "error"
        )

        return NudgeSignals(
            tool_calls_ok=tool_calls_ok,
            task_outcome=str(data.get("task_outcome", "")),
            error_count=error_count,
            user_correction_detected=bool(data.get("user_correction_detected", False)),
            max_existing_skill_coverage=float(data.get("max_existing_skill_coverage", 0.0)),
            turn_number=int(data.get("turn_number", 0)),
            trace_id=str(data.get("trace_id", "")),
        )

    # ------------------------------------------------------------------
    # Trigger evaluation
    # ------------------------------------------------------------------

    def _evaluate_trigger(self, signals: NudgeSignals) -> bool:
        """Evaluate the AND-conjunction from SDD §3.7.

        All conditions must be True:
          1. >= 5 successful tool calls
          2. task_outcome == "success"
          3. At least one of: error_count >= 1, user_correction, low coverage
          4. Not in cooldown (50-turn window)
          5. No exempt tags on current skills (checked externally via tags)
        """
        if signals.tool_calls_ok < _MIN_TOOL_CALLS_OK:
            return False
        if signals.task_outcome != "success":
            return False
        novelty = (
            signals.error_count >= 1
            or signals.user_correction_detected
            or signals.max_existing_skill_coverage < _MAX_EXISTING_COVERAGE
        )
        if not novelty:
            return False
        if self._in_global_cooldown(signals.turn_number):
            return False
        return True

    # ------------------------------------------------------------------
    # Cooldown helpers
    # ------------------------------------------------------------------

    def _in_global_cooldown(self, current_turn: int) -> bool:
        """True if a nudge was emitted within the last trace_buffer_turns turns."""
        # Remove expired entries (older than cooldown window)
        window = self._config.trace_buffer_turns
        while self._nudge_turns and (current_turn - self._nudge_turns[0]) >= window:
            self._nudge_turns.popleft()
        return len(self._nudge_turns) > 0

    def _is_shape_suppressed(self, tool_seq_hash: str, current_turn: int) -> bool:
        """True if this specific tool-shape was nudged within cooloff_turns."""
        until_turn = self._shape_suppressed_until.get(tool_seq_hash)
        if until_turn is None:
            return False
        return current_turn < until_turn

    def _record_nudge(self, turn_number: int, tool_seq_hash: str) -> None:
        """Update cooldown state after a successful nudge emission."""
        self._nudge_turns.append(turn_number)
        self._shape_suppressed_until[tool_seq_hash] = (
            turn_number + self._config.cooloff_turns
        )
        self._session_nudge_count += 1

    # ------------------------------------------------------------------
    # Nudge emission
    # ------------------------------------------------------------------

    async def _emit_nudge(
        self,
        signals: NudgeSignals,
        tool_names: list[str],
        tool_seq_hash: str,
        ctx: EventContext,
    ) -> None:
        """Publish system_message_nudge event and emit telemetry audit."""
        coverage_pct = signals.max_existing_skill_coverage
        nudge_text = _NUDGE_TEMPLATE.format(
            n_tools=signals.tool_calls_ok,
            coverage_pct=coverage_pct,
        )

        signal_vector: dict[str, Any] = {
            "tool_calls_ok": signals.tool_calls_ok,
            "task_outcome": signals.task_outcome,
            "error_count": signals.error_count,
            "user_correction_detected": signals.user_correction_detected,
            "max_existing_skill_coverage": signals.max_existing_skill_coverage,
        }

        # Publish advisory nudge for context_manager to inject on next turn.
        # Run as a tracked background task so we don't block the post_plan chain.
        # Holding a strong reference in _pending_tasks prevents premature GC.
        if self._bus is not None:
            task = asyncio.create_task(
                self._bus.emit(
                    "system_message_nudge",
                    {
                        "text": nudge_text,
                        "source": "skill_improver.nudge",
                        "turn_number": signals.turn_number,
                        "trace_id": signals.trace_id,
                    },
                    agent_did=ctx.agent_did,
                    trace_id=ctx.trace_id,
                )
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

        # Audit trail (NIST AU-3)
        if self._telemetry is not None:
            self._telemetry.audit_event(
                "skill_improver.nudge_emitted",
                {
                    "turn_id": signals.trace_id,
                    "session_id": self._session_id,
                    "signal_vector": signal_vector,
                    "tool_sequence_hash": tool_seq_hash,
                    "outcome_source": ctx.data.get("outcome_source", "heuristic"),
                },
            )

        _logger.info(
            "Nudge emitted: session=%s turn=%d tools_ok=%d coverage=%.2f",
            self._session_id,
            signals.turn_number,
            signals.tool_calls_ok,
            coverage_pct,
        )

    def _emit_dedup_event(
        self,
        signals: NudgeSignals,
        tool_seq_hash: str,
        reason: str,
        ctx: EventContext,
    ) -> None:
        """Emit audit event when nudge is suppressed by dedup."""
        if self._telemetry is not None:
            self._telemetry.audit_event(
                "skill_improver.nudge_dedup_hit",
                {
                    "turn_id": signals.trace_id,
                    "session_id": self._session_id,
                    "reason": reason,
                    "tool_sequence_hash": tool_seq_hash,
                },
            )

    # ------------------------------------------------------------------
    # Exempt tag check (called externally by module wiring)
    # ------------------------------------------------------------------

    def check_exempt_tags(self, skill_tags: list[str]) -> bool:
        """Return True if any tag is in the exempt list (blocks nudge).

        Called by the module startup context to check active skill tags
        before registering the nudge handler for a given skill scope.
        """
        exempt_set = set(self._config.exempt_tags)
        return bool(exempt_set & set(skill_tags))

    # ------------------------------------------------------------------
    # Knowledge injection (for dedup)
    # ------------------------------------------------------------------

    def update_known_skills(
        self,
        *,
        names: set[str],
        fingerprints: set[str],
        tool_lists: list[list[str]],
    ) -> None:
        """Inject current known-skill state for dedup comparisons.

        Called by the module host (SkillImproverModule) after skill registry
        changes so that dedup is always up-to-date without querying storage
        on every turn.
        """
        self._known_skill_names = names
        self._known_fingerprints = fingerprints
        self._known_tool_lists = tool_lists

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_proposed_name(tool_names: list[str]) -> str:
        """Derive a canonical skill name from the tool sequence.

        Produces a deterministic, path-safe name from the sorted tool set.
        Example: ["read", "bash", "grep"] -> "skill-bash-grep-read"
        """
        if not tool_names:
            return "skill-unnamed"
        sorted_names = sorted(set(tool_names))[:_MAX_TOOL_SUFFIX_TOKENS]
        slug = "-".join(t.lower().replace("_", "-") for t in sorted_names)
        return f"skill-{slug}"[:_MAX_SLUG_LENGTH]

    @property
    def session_nudge_count(self) -> int:
        """Total nudges emitted in this session (for test assertions)."""
        return self._session_nudge_count

    @property
    def nudge_turns(self) -> list[int]:
        """Copy of cooldown deque (for test assertions)."""
        return list(self._nudge_turns)

    @property
    def shape_suppressed_until(self) -> dict[str, int]:
        """Copy of per-shape suppression map (for test assertions)."""
        return dict(self._shape_suppressed_until)
