"""Shared utilities for ArcAgent."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from arcllm import LLMProvider
from arcllm import load_model as arcllm_load_model

_logger = logging.getLogger("arcagent.utils")


def load_eval_model(
    model_id: str,
    *,
    agent_label: str | None = None,
    trace_store: Any | None = None,
    on_event: Callable[[Any], None] | None = None,
) -> LLMProvider:
    """Load LLM model via ArcLLM for eval/background use.

    Parses ``provider/model`` format and delegates to ``arcllm.load_model()``.
    Shared by agent.py and memory module to avoid DRY violation.

    Always enables retry wrapping so transient connection errors
    (ConnectTimeout, 429, 5xx) are retried with exponential backoff.
    Scheduled and background tasks are especially prone to transient
    network issues — retry prevents single-failure schedule trips.

    Args:
        model_id: Provider/model identifier (e.g., "ollama/glm-5:cloud").
        agent_label: Label for trace attribution (e.g., "my_agent/eval").
        trace_store: Optional TraceStore for persistent LLM call recording.
        on_event: Optional callback fired after every invoke() with a
            TraceRecord. Enables arcagent to bridge ArcLLM events
            (llm_call, config_change, circuit_change) onto the ModuleBus
            so modules (ui_reporter, memory) observe LLM activity.
    """
    _logger.info("Loading model: %s (label=%s)", model_id, agent_label)
    provider, _, model_name = model_id.partition("/")
    return arcllm_load_model(
        provider, model_name or None, retry=True, agent_label=agent_label,
        trace_store=trace_store, on_event=on_event,
    )
