"""Shared eval model and background task utilities.

Eliminates DRY violation between PolicyModule and MarkdownMemoryModule.
Both modules use identical patterns for lazy eval model loading and
fire-and-forget background task management.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.utils import load_eval_model

_logger = logging.getLogger("arcagent.utils.model_helpers")


def get_eval_model(
    *,
    cached_model: Any,
    eval_config: EvalConfig,
    llm_config: Any | None,
    logger: logging.Logger = _logger,
    agent_label: str | None = None,
) -> Any:
    """Lazy-init eval model from config, fallback to agent's LLM config.

    Respects ``EvalConfig.fallback_behavior``:
    - ``"skip"``: return None on failure (default)
    - ``"error"``: raise on failure

    Args:
        cached_model: Previously loaded model (returned as-is if not None).
        eval_config: Eval model configuration.
        llm_config: Fallback LLM config if eval provider/model not set.
        logger: Logger instance for diagnostics.
        agent_label: Label for trace attribution (e.g., "my_agent/eval").

    Returns the loaded model or None.
    """
    if cached_model is not None:
        return cached_model

    if eval_config.provider and eval_config.model:
        model_id = f"{eval_config.provider}/{eval_config.model}"
    elif llm_config is not None:
        model_id = llm_config.model
    else:
        if eval_config.fallback_behavior == "error":
            msg = "No eval model config and no LLM config fallback"
            raise RuntimeError(msg)
        logger.warning("No eval model config and no LLM config fallback")
        return None

    try:
        return load_eval_model(model_id, agent_label=agent_label)
    except Exception:
        if eval_config.fallback_behavior == "error":
            raise
        logger.exception("Failed to load eval model: %s", model_id)
        return None


def spawn_background(
    coro: Coroutine[Any, Any, None],
    *,
    background_tasks: set[asyncio.Task[None]],
    semaphore: asyncio.Semaphore,
    eval_config: EvalConfig,
    telemetry: Any | None = None,
    audit_event_name: str = "background_error",
    logger: logging.Logger = _logger,
) -> None:
    """Fire-and-forget with semaphore, timeout, backpressure, and logging.

    Uses ``eval_config.background_queue_size`` and
    ``eval_config.background_task_timeout`` for configurable limits.
    """
    max_queue = eval_config.background_queue_size
    timeout = eval_config.background_task_timeout

    if len(background_tasks) >= max_queue:
        logger.warning(
            "Background task queue full (%d), dropping task",
            max_queue,
        )
        coro.close()
        return

    async def _semaphore_wrapped() -> None:
        async with semaphore:
            await asyncio.wait_for(coro, timeout=timeout)

    task = asyncio.create_task(_semaphore_wrapped())
    background_tasks.add(task)

    def _on_done(t: asyncio.Task[None]) -> None:
        background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc and telemetry is not None:
            telemetry.audit_event(
                audit_event_name,
                {
                    "error": str(exc),
                    "type": type(exc).__name__,
                },
            )

    task.add_done_callback(_on_done)
