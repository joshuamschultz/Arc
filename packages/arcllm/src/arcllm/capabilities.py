"""Model capability discovery — answers "can this model carry tool calls?".

The per-model ``supports_tools`` flag has lived in provider TOML metadata
forever; what's been missing is a stable public way for callers to ask
the question before they wire a tool-using agent on top of a non-tool
model. Without this, the only signal a demo gets is silent failure
(``tool_calls_made=0`` every turn, JSON-as-text content) which costs an
hour of debugging.

Two entry points:

- ``supports_tools(provider, model)`` — pre-check before constructing
  an agent. Returns ``True`` only when the provider TOML metadata
  explicitly says so; unknown models default to ``False`` (fail-closed).

- ``tool_capable_models(provider)`` — enumerate the models for a
  provider that the registry believes can carry tool calls. Useful for
  CLIs that want to surface a picker.

The companion enforcement runs inside the adapter — see
``BaseAdapter._check_tool_capability`` — and converts a wrong-model
choice from silent failure into ``ArcLLMConfigError`` at invoke time.
"""

from __future__ import annotations

import logging

from arcllm.config import load_provider_config

_logger = logging.getLogger(__name__)


def supports_tools(provider: str, model: str) -> bool:
    """Return True iff provider TOML metadata declares ``supports_tools = true``.

    Fail-closed: unknown providers, unknown models, and provider TOMLs
    that omit the field all return ``False``. Callers seeing ``False``
    should choose a different model rather than gamble on adapter
    behavior.
    """
    try:
        config = load_provider_config(provider)
    except Exception:  # reason: fail-closed — log + return False
        _logger.debug("supports_tools: could not load provider %r", provider, exc_info=True)
        return False
    meta = config.models.get(model)
    if meta is None:
        return False
    return bool(meta.supports_tools)


def tool_capable_models(provider: str) -> list[str]:
    """Return the model names this provider's TOML marks as tool-capable.

    Empty list on unknown providers or load failures. The result is the
    same list ``arcllm.load_model(provider, model=...)`` will accept for
    tool-using agents.
    """
    try:
        config = load_provider_config(provider)
    except Exception:  # reason: fail-closed — log + return empty
        _logger.debug(
            "tool_capable_models: could not load provider %r", provider, exc_info=True
        )
        return []
    return sorted(name for name, meta in config.models.items() if meta.supports_tools)
