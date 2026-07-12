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
  open loops (the :data:`CONTEXT_MAINTAINER_SYSTEM_PROMPT` persona).
* ``agent:shutdown`` (priority 60) — drain in-flight maintainer tasks.

Every rewrite is sanitized (ASI-06) and written atomically so a concurrent
per-run read never sees a half-written file. Fail-open throughout: a maintainer
error must never disturb the response path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from arcllm.types import Message

from arcagent.modules.workpad import _runtime
from arcagent.modules.workpad.prompt import CONTEXT_MAINTAINER_SYSTEM_PROMPT
from arcagent.tools._decorator import hook
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
    """Count non-automated runs, accumulate transcript, rewrite every N runs.

    Automated runs (pulse / scheduler) are session activity but not user-facing
    interactions; counting them would make "every 20 runs" unpredictable and
    burn eval tokens on background churn, so they are skipped (parity with the
    policy eval cadence).
    """
    if ctx.data.get("automated", False):
        return
    st = _runtime.state()
    _accumulate(st, ctx.data.get("messages", []))
    st.run_count += 1
    if st.run_count % st.config.every_n_runs != 0:
        return
    model = _eval_model()
    if model is None:
        return
    transcript_text = _drain_transcript(st)
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
        content = raw.strip() if isinstance(raw, str) else ""
        if content:
            st.transcript.append(f"[{role}] {content}")
    _trim_transcript(st)


def _trim_transcript(st: _runtime._State) -> None:
    """Drop oldest lines while the accumulated transcript exceeds its budget."""
    total = sum(len(line) for line in st.transcript)
    while len(st.transcript) > 1 and total > st.config.max_transcript_chars:
        total -= len(st.transcript.pop(0))


def _drain_transcript(st: _runtime._State) -> str:
    """Snapshot the accumulated transcript and clear it for the next window."""
    text = "\n".join(st.transcript)
    st.transcript = []
    return text


# -- Maintenance ---------------------------------------------------------


async def _safe_maintain(st: _runtime._State, model: Any, transcript_text: str) -> None:
    """Fail-open wrapper: a maintenance error must not surface as a task failure."""
    try:
        await perform_maintenance(st, model, transcript_text)
    except Exception:  # reason: fail-open — a rewrite error must not disturb the agent
        _logger.warning("workpad maintenance failed", exc_info=True)


async def perform_maintenance(st: _runtime._State, model: Any, transcript_text: str) -> bool:
    """Rewrite ``context.md`` from its current content + recent activity.

    Returns whether the file was written. Empty/whitespace model output leaves the
    existing file untouched (never blank the cockpit on a degenerate response).
    """
    context_path = st.workspace / "context.md"
    current = context_path.read_text(encoding="utf-8") if context_path.exists() else ""

    response = await model.invoke(
        [
            Message(role="system", content=CONTEXT_MAINTAINER_SYSTEM_PROMPT),
            Message(role="user", content=_render_input(current, transcript_text)),
        ]
    )
    new_md = (response.content or "").strip()
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


def _render_input(current_context: str, transcript_text: str) -> str:
    """Render the maintainer's user turn: current file + recent activity."""
    return (
        "CURRENT context.md (may be empty):\n"
        f"{current_context.strip() or '(empty)'}\n\n"
        "RECENT SESSION ACTIVITY since the last update:\n"
        f"{transcript_text.strip() or '(none)'}\n\n"
        "Rewrite context.md now per your maintenance rules. Output ONLY the full "
        "updated context.md content — no preamble, no explanation, no code fences."
    )


def _atomic_write(path: Any, content: str) -> None:
    """Write via a temp file + rename so a concurrent per-run read is never torn."""
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "CONTEXT_MAINTAINER_SYSTEM_PROMPT",
    "drain_on_shutdown",
    "perform_maintenance",
    "track_runs",
]
