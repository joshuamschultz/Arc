"""Decorator-form policy module — SPEC-021 task 3.7.

Three ``@hook`` functions that mirror :class:`PolicyModule`'s
``startup`` registrations:

  * ``agent:assemble_prompt``  (priority 60) — inject ``policy.md``.
  * ``agent:post_respond``     (priority 110) — periodic policy eval.
  * ``agent:shutdown``         (priority 60) — terminal eval + drain.

State is shared via :mod:`arcagent.modules.policy._runtime`. The agent
configures it once at startup; the hooks read state lazily.

The legacy :class:`PolicyModule` class still exists alongside this
module to keep its existing test surface working; both forms route to
the same :class:`PolicyEngine` instance internally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arcagent.modules.policy import _runtime
from arcagent.tools._decorator import hook
from arcagent.utils.model_helpers import get_eval_model, spawn_background

_logger = logging.getLogger("arcagent.modules.policy.capabilities")


def _eval_model() -> Any:
    """Lazy-init eval model with cache via ``_runtime.state``."""
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


@hook(event="agent:assemble_prompt", priority=60)
async def inject_policy_md(ctx: Any) -> None:
    """Inject ``policy.md`` content into the prompt's sections dict."""
    sections = ctx.data.get("sections")
    if sections is None or not isinstance(sections, dict):
        return
    st = _runtime.state()
    policy_path = st.workspace / "policy.md"
    if policy_path.exists():
        content = policy_path.read_text(encoding="utf-8").strip()
        if content:
            sections["policy"] = content


@hook(event="agent:post_respond", priority=110)
async def periodic_policy_eval(ctx: Any) -> None:
    """Fire policy eval every ``eval_interval_turns`` turns.

    Skips automated runs (no session_id) so transient tool errors
    from pulse / scheduler don't pollute learned policies.
    """
    session_id = ctx.data.get("session_id", "")
    if not session_id:
        return
    model = _eval_model()
    if model is None:
        return
    messages = ctx.data.get("messages", [])
    if not messages:
        return

    st = _runtime.state()
    st.session_messages = messages
    st.turn_count += 1
    if st.turn_count % st.config.eval_interval_turns != 0:
        return
    if st.telemetry is not None:
        st.telemetry.audit_event(
            "policy.eval_triggered",
            {"turn": st.turn_count, "session_id": session_id},
        )
    spawn_background(
        _safe_evaluate(messages, model, session_id=session_id),
        background_tasks=st.background_tasks,
        semaphore=st.semaphore,
        eval_config=st.eval_config,
        telemetry=st.telemetry,
        audit_event_name="policy.background_error",
        logger=_logger,
    )


@hook(event="agent:shutdown", priority=60)
async def terminal_policy_eval(ctx: Any) -> None:
    """Run a final policy eval on session-end, then drain background tasks."""
    st = _runtime.state()
    if st.session_messages:
        model = _eval_model()
        if model is not None:
            session_id = ctx.data.get("session_id", "")
            await _safe_evaluate(st.session_messages, model, session_id=session_id)
    if st.background_tasks:
        _logger.info(
            "Cancelling %d policy background task(s) for shutdown",
            len(st.background_tasks),
        )
        for task in st.background_tasks:
            task.cancel()
        await asyncio.gather(*st.background_tasks, return_exceptions=True)


async def _safe_evaluate(
    messages: list[dict[str, Any]],
    model: Any,
    *,
    session_id: str = "",
) -> None:
    """Evaluate respecting ``fallback_behavior``."""
    st = _runtime.state()
    try:
        await st.engine.evaluate(messages, model, session_id=session_id)
    except Exception:
        if st.eval_config.fallback_behavior == "error":
            raise
        _logger.warning("Policy evaluation error, skipping", exc_info=True)
        if st.telemetry is not None:
            st.telemetry.audit_event(
                "policy.eval_skipped",
                {"session_id": session_id, "reason": "evaluation_error"},
            )
