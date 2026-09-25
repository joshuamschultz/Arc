"""Thin skills wiring — the only arcagent-side improver code (SPEC-044).

This module holds no improvement logic; it forwards *primitive* per-turn signals to
the config-selected :class:`~arcagent.skilladapt.SkillAdapter`:

* ``agent:post_tool``   — detect a skill read (open the active span), then forward each
  subsequent tool call as ``observe`` (the signal-extraction half of the old
  ``trace_collector``);
* ``agent:post_plan``   — ``on_turn_end`` closes the span + accrues usage, and stashes the
  turn number for the off-loop Curator sweep;
* ``agent:pre_respond`` — ``maybe_improve`` triggers the gated improvement pass, threading
  any recurring-failure ``insight`` the memory module produced this turn (REQ-060);
* ``agent:ready``       — index skill paths from the CapabilityRegistry and rehydrate the
  retire/revive suppression set;
* a ``@background_task`` loop — drives the Curator lifecycle sweep on a config cadence.

With a :class:`~arcagent.skilladapt.NullSkillAdapter` selected, ``state().active`` is
``False`` and every hook short-circuits — a silent no-op that writes nothing (AC-1).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arcagent.modules.skills import _runtime
from arcagent.tools._decorator import background_task, hook
from arcagent.tools._secret_guard import find_secret
from arcagent.utils.periodic import PeriodicRunner

_logger = logging.getLogger("arcagent.modules.skills.capabilities")

# @background_task interval is metadata only; the loop owns its own sleep, reading the
# live cadence from config each cycle (default hourly).
_SWEEP_POLL_DEFAULT = 3_600.0
_MAX_OBSERVATION_ARGS_BYTES = 4_096
_CREDENTIAL_KEYS = (
    "password",
    "secret",
    "token",
    "credential",
    "api_key",
    "private_key",
    "authorization",
    "cookie",
)


def _call_status(ctx: Any) -> tuple[str, str | None]:
    """Derive (status, error_type) from an EventContext result."""
    if getattr(ctx, "is_vetoed", False):
        return "vetoed", None
    explicit = ctx.data.get("status")
    if explicit in {"error", "timeout"}:
        error_type = ctx.data.get("error_type")
        return "error", error_type if isinstance(error_type, str) else "ToolError"
    if explicit in {"cancelled", "replayed"}:
        return explicit, None
    if explicit == "ok":
        return "ok", None
    if explicit is not None:
        return "invalid", None
    result = ctx.data.get("result")
    if isinstance(result, Exception):
        return "error", type(result).__name__
    return "ok", None


def _safe_observation_args(args: Any) -> dict[str, Any] | None:
    """Omit credentials before any skill adapter can persist tool arguments."""
    if not isinstance(args, dict) or not args:
        return None
    try:
        serialized = json.dumps(args)
    except (TypeError, ValueError):
        return None
    if len(serialized.encode("utf-8")) > _MAX_OBSERVATION_ARGS_BYTES:
        return None
    safe = json.loads(serialized)
    if not isinstance(safe, dict):
        return None
    if _has_credential_key(safe) or find_secret(serialized):
        return None
    return safe


def _has_credential_key(value: Any) -> bool:
    """Find sensitive field names in nested tool arguments."""
    if isinstance(value, dict):
        return any(
            any(part in str(key).lower() for part in _CREDENTIAL_KEYS) or _has_credential_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_credential_key(item) for item in value)
    return False


@hook(event="agent:post_tool", priority=200)
async def skills_post_tool(ctx: Any) -> None:
    """Detect skill reads; forward subsequent tool calls to the adapter as observations."""
    st = _runtime.state()
    if not st.active:
        return
    session_id = str(ctx.data.get("session_id") or "")
    run_id = str(ctx.data.get("run_id") or "")
    turn_state = st.turn(session_id, run_id)
    if turn_state is None:
        return
    turn_number = ctx.data.get("turn_number")
    if ctx.data.get("source") == "arcrun" and not isinstance(turn_number, int):
        return
    if isinstance(turn_number, int) and turn_state.turn_number not in (None, turn_number):
        return
    tool = ctx.data.get("tool", "")
    status, error_type = _call_status(ctx)
    if status in {"cancelled", "replayed", "invalid", "vetoed"}:
        return
    call_id = ctx.data.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        _logger.warning("Skill tool outcome missing call_id")
        return
    if not turn_state.accept_call(call_id):
        return

    if tool == "read":
        if status != "ok":
            return
        args = ctx.data.get("args")
        file_path = args.get("file_path", "") if isinstance(args, dict) else ""
        if file_path:
            try:
                resolved = Path(file_path).resolve()
            except (ValueError, OSError):
                return
            skill_name = st.skill_paths.get(resolved)
            if skill_name is not None:
                turn_state.active_skill = skill_name
        return

    if turn_state.active_skill is not None and tool:
        if status == "error":
            turn_state.error_counts[turn_state.active_skill] = (
                turn_state.error_counts.get(turn_state.active_skill, 0) + 1
            )
        await st.adapter.observe(
            skill_name=turn_state.active_skill,
            tool_name=tool,
            status=status,
            error_type=error_type,
            args=_safe_observation_args(ctx.data.get("args")),
            session_id=session_id or None,
            run_id=run_id or None,
            call_id=call_id,
            llm_trace_id=turn_state.llm_trace_id,
        )


@hook(event="agent:post_plan", priority=200)
async def skills_post_plan(ctx: Any) -> None:
    """Close the active span at turn end, stash the turn, and clear the active-skill tracker.

    With a configured OutcomeClassifier (SPEC-054 REQ-115/116), the turn's messages are
    classified and a non-abstaining label supersedes the raw ``task_outcome`` — the
    producer for the trace store's ``outcome_source='evaluator'`` slot.
    """
    st = _runtime.state()
    if not st.active:
        return
    session_id = str(ctx.data.get("session_id") or "")
    run_id = str(ctx.data.get("run_id") or "")
    turn_state = st.turn(session_id, run_id)
    if turn_state is None:
        return
    outcome = str(ctx.data.get("task_outcome", ""))
    turn = int(ctx.data.get("turn_number", 0))
    if turn_state.turn_number is not None and turn_state.turn_number != turn:
        return
    _runtime.record_turn(turn)
    outcome = await _classify_outcome(st, turn_state, ctx) or outcome
    await st.adapter.on_turn_end(
        turn=turn, outcome=outcome, session_id=session_id or None, run_id=run_id or None
    )
    st.close_turn(session_id, run_id)


@hook(event="agent:pre_plan", priority=200)
async def skills_pre_plan(ctx: Any) -> None:
    """Open the next turn of a run after its previous span was closed."""
    st = _runtime.state()
    if st.active:
        number = ctx.data.get("turn_number")
        st.begin_turn(
            str(ctx.data.get("session_id") or ""),
            str(ctx.data.get("run_id") or ""),
            number if isinstance(number, int) else None,
        )


@hook(event="llm:call_complete", priority=200)
async def skills_llm_call_complete(ctx: Any) -> None:
    """Stash the current turn's arcllm trace id so this turn's tool observations can link it.

    The arcllm bridge (``arcagent.core.model_manager``) emits ``llm:call_complete`` from every
    ``TraceRecord``, carrying the ``trace_id`` arcllm PERSISTED the request/response payload
    under. It fires when the invoke that produced the turn's tool calls completes — before
    those tools run and their ``agent:post_tool`` events fire — so ``skills_post_tool`` reads a
    live id and forwards it as ``observe(llm_trace_id=...)``. That is the producer half of the
    read-time curation join (H-041): the span records the id; the join resolves the payload
    from arcllm's store at read time, never copying a body into a second store.
    """
    st = _runtime.state()
    if not st.active:
        return
    trace_id = ctx.data.get("trace_id")
    if trace_id:
        turn_state = st.turn(
            str(ctx.data.get("session_id") or ""), str(ctx.data.get("run_id") or "")
        )
        if turn_state is not None:
            turn_state.llm_trace_id = str(trace_id)


async def _classify_outcome(st: _runtime._State, turn_state: _runtime._TurnState, ctx: Any) -> str:
    """Consult the turn-end classifier; '' when unconfigured, signal-less, or failing.

    Fail-open (REQ-115): classification is a background labeler — an exception must
    never break turn close. Skipped without messages or an active-skill context, since
    a label could not be attributed anyway.
    """
    messages = ctx.data.get("messages") or []
    if st.outcome_classifier is None or not messages or turn_state.active_skill is None:
        return ""
    try:
        label = await st.outcome_classifier.classify(
            transcript_window=messages,
            active_skills=[turn_state.active_skill],
            error_counts=dict(turn_state.error_counts),
        )
    except Exception:  # reason: fail-open — labeling must never break turn end
        _logger.warning("turn-end outcome classification failed", exc_info=True)
        return ""
    return label.outcome


@hook(event="agent:pre_respond", priority=150)
async def skills_pre_respond(ctx: Any) -> None:
    """Trigger the gated improvement pass for over-threshold skills.

    ``insight`` is the optional recurring-failure abstraction the memory module's
    ``agent:pre_respond`` hook (priority 100, runs first) placed on ``ctx.data`` from the
    active Brain's retrieval; empty when memory is off (the improver works memory-less).
    """
    st = _runtime.state()
    if not st.active:
        return
    insight = str(ctx.data.get("insight", ""))
    await st.adapter.maybe_improve(insight=insight)


@hook(event="agent:ready", priority=100)
async def skills_ready(ctx: Any) -> None:
    """Index skill paths from the CapabilityRegistry and rehydrate retire suppression."""
    st = _runtime.state()
    if not st.active:
        return
    registry = ctx.data.get("skill_registry") or st.skill_registry
    if registry is None:
        _logger.warning("no skill_registry in agent:ready; skill trace attribution disabled")
        return
    st.index_skills(registry)
    # Rehydrate: re-suppress skills retired in a prior session (read from the on-disk
    # candidate-store manifest) so retirement survives restart (HIGH-3).
    await _runtime.reconcile_suppression()


@background_task(name="skills_review_lifecycle_loop", interval=_SWEEP_POLL_DEFAULT)
async def skills_review_lifecycle_loop(_ctx: Any) -> None:
    """Curator: periodically sweep retire/revive through the adapter (CRITICAL-1 producer).

    The decorator interval is metadata; the loop owns its cadence, reading the live
    ``sweep_poll_seconds`` config each cycle. Mirrors the memory consolidate loop.
    """

    def on_error(exc: BaseException, _failures: int) -> None:
        _logger.warning("skills lifecycle sweep failed: %s", exc)

    await PeriodicRunner().run(
        _runtime.run_lifecycle_sweep,
        interval=_poll_interval,
        on_error=on_error,
    )


def _poll_interval() -> float:
    """The live sweep-poll cadence, or the default when the module is unconfigured."""
    try:
        return _runtime.state().sweep_poll_seconds
    except RuntimeError:
        return _SWEEP_POLL_DEFAULT


__all__ = [
    "skills_llm_call_complete",
    "skills_post_plan",
    "skills_post_tool",
    "skills_pre_plan",
    "skills_pre_respond",
    "skills_ready",
    "skills_review_lifecycle_loop",
]
