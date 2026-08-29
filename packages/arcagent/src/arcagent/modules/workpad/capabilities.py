"""Workpad wiring — the self-managing ``context.md`` maintainer.

``context.md`` is injected into every system prompt by the core
(:class:`~arcagent.core.session_internal.context.ContextManager`), and re-read
from disk at the top of every run — so a background rewrite lands automatically
on the next run with no hot-reload wiring.

This module is the SOLE writer of that file. Compaction no longer flushes to it
(that mixed a durable-curation concern into message-history management). Instead:

* ``agent:post_respond`` (priority 120) — count real (non-automated) runs and
  accumulate the turn's transcript; every ``every_n_runs`` runs, spawn a
  background eval-model call that rewrites ``context.md`` as a curated cockpit of
  open loops (the ``context_maintainer_system`` stock-prompt persona).
* ``agent:shutdown`` (priority 60) — drain in-flight maintainer tasks.

Every rewrite is sanitized (ASI-06) and written atomically so a concurrent
per-run read never sees a half-written file. Fail-open throughout: a maintainer
error must never disturb the response path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date
from typing import Any

import arcrun
from arcokf import OKFValidationError, validate
from arcprompt import load_stock

from arcagent.core import turn_context
from arcagent.modules.workpad import _runtime
from arcagent.tools._decorator import hook, tool
from arcagent.utils.audit import safe_audit
from arcagent.utils.model_helpers import get_eval_model, spawn_background
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.workpad.capabilities")

_TRACK_PRIORITY = 120
_SHUTDOWN_PRIORITY = 60


def _eval_model() -> Any:
    """Lazy-init the eval model, cached on the runtime state (mirrors policy)."""
    st = _runtime.state()
    result = get_eval_model(
        cached_model=st.eval_model,
        eval_config=st.eval_config,
        llm_config=st.llm_config,
        logger=_logger,
        agent_label=st.eval_label,
    )
    if result is not None:
        st.eval_model = result
    return result


# -- Run tracking + cadence ----------------------------------------------


@hook(event="agent:post_respond", priority=_TRACK_PRIORITY)
async def track_runs(ctx: Any) -> None:
    """Count non-automated runs, accumulate transcript, rewrite on the cadence.

    ``automated=True`` marks only the checkpoint-resume turn and the tracked-run
    finalizer — background churn that should not count as a user interaction.
    Pulse, scheduler, and task runs go through ``dispatch_stream`` with
    ``automated=False`` and ARE counted toward the cadence (the desired behavior:
    that work still belongs in the context cockpit).
    """
    if ctx.data.get("automated", False):
        return
    st = _runtime.state()
    # Ignore background self-wakes — the pulse tick, the proactive scheduler,
    # memory consolidation, and the workpad's own maintenance run. They are not
    # real context to curate and must not drive the cadence; only a turn a person
    # drove counts. (See ADR: background runs never drive maintenance cadence.)
    if turn_context.interactive():
        _accumulate(st, ctx.data.get("messages", []))
        st.run_count += 1
        st.last_activity_ts = time.time()
        st.persist()
    # The maintenance CHECK still runs on any turn, so a later background turn can
    # carry the idle flush once enough quiet time has passed since the last real one.
    if not _should_maintain(st):
        return
    model = _eval_model()
    if model is None:
        return
    transcript_text = _drain_transcript(st)
    st.last_maintenance_ts = time.time()
    st.runs_at_last_maintenance = st.run_count
    st.persist()
    await safe_audit(
        st.telemetry,
        "workpad.triggered",
        {"run_count": st.run_count},
        logger=_logger,
    )
    if st.semaphore is None:
        raise RuntimeError("workpad runtime not configured: semaphore missing")
    spawn_background(
        _safe_maintain(st, model, transcript_text),
        background_tasks=st.background_tasks,
        semaphore=st.semaphore,
        eval_config=st.eval_config,
        telemetry=st.telemetry,
        audit_event_name="workpad.background_error",
        logger=_logger,
    )


def _should_maintain(st: _runtime._State) -> bool:
    """Fire only on unflushed REAL activity — a person's turns, never background churn.

    Gated first on there being new real activity since the last rewrite, so a quiet
    agent whose only turns are background self-wakes never maintains. Then either the
    cadence boundary (a long active session) or ~``flush_idle_seconds`` since the last
    REAL run (the person stopped) triggers a single flush. Idle is measured from
    ``last_activity_ts`` (the last real run), not the last maintenance, so cleanup
    lands after the person actually went quiet rather than on a rolling schedule.
    """
    if st.run_count <= st.runs_at_last_maintenance:
        return False
    if st.run_count % st.config.every_n_runs == 0:
        return True
    return time.time() - st.last_activity_ts >= st.config.flush_idle_seconds


@hook(event="agent:shutdown", priority=_SHUTDOWN_PRIORITY)
async def drain_on_shutdown(_ctx: Any) -> None:
    """Cancel and await in-flight maintainer tasks on session end."""
    st = _runtime.state()
    if not st.background_tasks:
        return
    _logger.info("Cancelling %d workpad task(s) for shutdown", len(st.background_tasks))
    for task in st.background_tasks:
        task.cancel()
    await asyncio.gather(*st.background_tasks, return_exceptions=True)


# -- Transcript accumulation ---------------------------------------------


def _accumulate(st: _runtime._State, messages: list[Any]) -> None:
    """Append this turn's role-tagged content, then trim to the char budget."""
    for msg in messages:
        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
        raw = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        content = arcrun.content_text(raw).strip()
        if content:
            st.transcript.append(f"[{role}] {content}")
    _trim_transcript(st)


def _trim_transcript(st: _runtime._State) -> None:
    """Bound the accumulated transcript to its budget.

    Drop oldest lines first; then, if a single remaining line is itself over
    budget — a coding agent's turn can carry one enormous file or tool dump —
    truncate it. Without that last step a lone over-budget message passed
    straight through, which is what let a few-thousand-char budget resend a
    ~190k-token transcript to the workpad rewrite every run.
    """
    budget = st.config.max_transcript_chars
    total = sum(len(line) for line in st.transcript)
    while len(st.transcript) > 1 and total > budget:
        total -= len(st.transcript.pop(0))
    if st.transcript and len(st.transcript[-1]) > budget:
        line = st.transcript[-1]
        st.transcript[-1] = f"{line[:budget]}…[+{len(line) - budget} chars truncated]"


def _drain_transcript(st: _runtime._State) -> str:
    """Snapshot the accumulated transcript and clear it for the next window."""
    text = "\n".join(st.transcript)
    st.transcript = []
    return text


# -- Agent-requested rewrite ---------------------------------------------

# The notes are one tool argument, not a file: a bound keeps a single call from
# smuggling an entire replacement cockpit past the maintainer's judgement.
_MAX_NOTES_CHARS = 4000


@tool(
    name="workpad_update",
    description=(
        "Request an immediate curated rewrite of context.md — your working "
        "memory. context.md is maintained FOR you and is deliberately not "
        "writable with file tools; pass what to add, correct, or drop, and "
        "the maintainer rewrites the whole cockpit now."
    ),
    classification="state_modifying",
    capability_tags=["workpad"],
    when_to_use=(
        "Update or clean up context.md (your scratchpad): close finished "
        "loops, correct stale entries, or capture a new open loop right away "
        "instead of waiting for the automatic cadence."
    ),
)
async def workpad_update(notes: str) -> str:
    """Run the context.md maintainer now, with the agent's notes as input.

    The protected-file guard stays intact: the agent never writes the file.
    The same eval-model persona, sanitizer, size cap, and atomic write produce
    it — the notes only join the maintainer's evidence, alongside the
    accumulated activity window (drained here exactly as the cadence path
    drains it, so nothing is double-counted later).
    """
    st = _runtime.state()
    model = _eval_model()
    if model is None:
        return "workpad maintainer unavailable: no eval model is configured"
    clipped = sanitize_text(notes, max_length=_MAX_NOTES_CHARS, truncation_suffix="…")
    transcript_text = _drain_transcript(st)
    st.last_maintenance_ts = time.time()
    st.runs_at_last_maintenance = st.run_count
    st.persist()
    await safe_audit(
        st.telemetry,
        "workpad.requested",
        {"run_count": st.run_count, "notes_chars": len(clipped)},
        logger=_logger,
    )
    try:
        written = await perform_maintenance(st, model, transcript_text, agent_notes=clipped)
    except Exception as exc:  # reason: report failure to the agent, never crash the turn
        _logger.warning("agent-requested workpad rewrite failed", exc_info=True)
        return f"context.md rewrite failed: {exc}"
    if not written:
        return "context.md left unchanged: the maintainer produced no content"
    return (
        "context.md rewritten by the maintainer with your notes applied. "
        "It reloads into your prompt at the start of your next run."
    )


# -- Maintenance ---------------------------------------------------------


async def _safe_maintain(st: _runtime._State, model: Any, transcript_text: str) -> None:
    """Fail-open wrapper: a maintenance error must not surface as a task failure."""
    try:
        await perform_maintenance(st, model, transcript_text)
    except Exception:  # reason: fail-open — a rewrite error must not disturb the agent
        _logger.warning("workpad maintenance failed", exc_info=True)


async def perform_maintenance(
    st: _runtime._State, model: Any, transcript_text: str, *, agent_notes: str = ""
) -> bool:
    """Rewrite ``context.md`` from its current content + recent activity.

    Returns whether the file was written. Empty/whitespace model output leaves the
    existing file untouched (never blank the cockpit on a degenerate response).
    """
    context_path = st.workspace / "context.md"
    current = context_path.read_text(encoding="utf-8") if context_path.exists() else ""

    result = await arcrun.run_oneshot(
        model,
        system=load_stock("arcagent", "context_maintainer_system"),
        user=_render_input(current, transcript_text, agent_notes=agent_notes),
        max_tokens=None,
    )
    new_md = (result.content or "").strip()
    if not new_md:
        return False

    sanitized = sanitize_text(
        new_md,
        max_length=st.config.max_context_chars,
        truncation_suffix="\n[truncated]",
    )
    _atomic_write(context_path, sanitized + "\n")
    await safe_audit(
        st.telemetry,
        "workpad.updated",
        {"bytes": len(sanitized), "run_count": st.run_count},
        logger=_logger,
    )
    return True


def _render_input(current_context: str, transcript_text: str, *, agent_notes: str = "") -> str:
    """Render the maintainer's user turn: current file + recent activity.

    Today's date is supplied because the model has no clock: asked to stamp the
    file it invents one, and a wrong date makes every staleness judgement in the
    prompt wrong with it.
    """
    notes_block = (
        "THE AGENT'S OWN CURATION REQUEST for this rewrite (apply where it "
        f"matches reality per your rules):\n{agent_notes.strip()}\n\n"
        if agent_notes.strip()
        else ""
    )
    return (
        f"Today's date is {date.today().isoformat()}.\n\n"
        "CURRENT context.md (may be empty):\n"
        f"{current_context.strip() or '(empty)'}\n\n"
        "RECENT SESSION ACTIVITY since the last update:\n"
        f"{transcript_text.strip() or '(none)'}\n\n"
        f"{notes_block}"
        "Rewrite context.md now per your maintenance rules. Output ONLY the full "
        "updated context.md content — no preamble, no explanation, no code fences."
    )


def _atomic_write(path: Any, content: str) -> None:
    """Write via a temp file + rename so a concurrent per-run read is never torn."""
    result = validate(content, path=path.name)
    if not result.valid:
        raise OKFValidationError(result.diagnostics)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "drain_on_shutdown",
    "perform_maintenance",
    "track_runs",
    "workpad_update",
]
