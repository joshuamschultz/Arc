"""Shared utilities for ArcAgent."""

from __future__ import annotations

import logging

from arcllm import LLMProvider
from arcllm import load_model as arcllm_load_model

_logger = logging.getLogger("arcagent.utils")


def load_eval_model(model_id: str) -> LLMProvider:
    """Load LLM model via ArcLLM for eval/background use.

    Parses ``provider/model`` format and delegates to ``arcllm.load_model()``.
    Shared by agent.py and memory module to avoid DRY violation.
    """
    _logger.info("Loading model: %s", model_id)
    provider, _, model_name = model_id.partition("/")
    return arcllm_load_model(provider, model_name or None)
