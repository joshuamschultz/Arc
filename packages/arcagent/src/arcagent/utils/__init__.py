"""Shared utilities for ArcAgent."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from arcllm import MODULE_NAMES, LLMProvider
from arcllm import load_model as arcllm_load_model

_logger = logging.getLogger("arcagent.utils")


def load_eval_model(
    model_id: str,
    *,
    agent_label: str | None = None,
    agent_did: str | None = None,
    trace_store: Any | None = None,
    on_event: Callable[[Any], None] | None = None,
    arcllm_modules: dict[str, dict[str, Any]] | None = None,
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
        agent_did: Verified agent DID for trace attribution (task 27) — a
            trace's ``agent_did`` proves which agent's identity a call
            actually ran under; ``agent_label`` alone is free text.
        trace_store: Optional TraceStore for persistent LLM call recording.
        on_event: Optional callback fired after every invoke() with a
            TraceRecord. Enables arcagent to bridge ArcLLM events
            (llm_call, config_change, circuit_change) onto the ModuleBus
            so modules like memory observe LLM activity.
        arcllm_modules: Optional per-agent overrides for arcllm modules
            (``queue``, ``retry``, ``rate_limit``, …). Each entry is a dict
            merged into the corresponding module's config (see
            ``arcllm.load_model``). Unknown keys raise ``ValueError`` so
            ``[llm.modules.qeue]`` typos in ``arcagent.toml`` fail loudly
            instead of silently dropping the override.
    """
    _logger.info("Loading model: %s (label=%s)", model_id, agent_label)
    provider, _, model_name = model_id.partition("/")

    module_overrides: dict[str, Any] = {}
    if arcllm_modules:
        unknown = set(arcllm_modules) - MODULE_NAMES
        if unknown:
            raise ValueError(
                f"Unknown arcllm module key(s): {sorted(unknown)}. "
                f"Valid keys: {sorted(MODULE_NAMES)}.",
            )
        module_overrides.update(arcllm_modules)

    return arcllm_load_model(
        provider,
        model_name or None,
        retry=module_overrides.pop("retry", True),
        agent_label=agent_label,
        agent_did=agent_did,
        trace_store=trace_store,
        on_event=on_event,
        **module_overrides,
    )
