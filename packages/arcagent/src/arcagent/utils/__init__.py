"""Shared utilities for ArcAgent."""

from __future__ import annotations

import logging

from arcllm import LLMProvider
from arcllm import load_model as arcllm_load_model

_logger = logging.getLogger("arcagent.utils")


def load_eval_model(
    model_id: str,
    *,
    agent_label: str | None = None,
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
    """
    _logger.info("Loading model: %s (label=%s)", model_id, agent_label)
    provider, _, model_name = model_id.partition("/")
    return arcllm_load_model(
        provider, model_name or None, retry=True, agent_label=agent_label,
    )
