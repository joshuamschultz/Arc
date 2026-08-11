"""Public model boundary used by ArcRun consumers.

ArcRun owns the model-facing contract for layers above the execution engine.
This module deliberately exposes only the ArcLLM capabilities needed to build
and operate a run; provider adapters and ArcLLM implementation modules remain
below this boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias

import arcllm

ContentBlock = arcllm.ContentBlock
Delta = arcllm.Delta
ImageBlock = arcllm.ImageBlock
LLMProvider = arcllm.LLMProvider
LLMResponse = arcllm.LLMResponse
Message = arcllm.Message
ProviderKey = arcllm.ProviderKey
ResponseFormat = arcllm.ResponseFormat
StopReason = arcllm.StopReason
TextBlock = arcllm.TextBlock
ToolCall = arcllm.ToolCall
ToolCallDelta = arcllm.ToolCallDelta
ToolResultBlock = arcllm.ToolResultBlock
ToolUseBlock = arcllm.ToolUseBlock
TraceStore: TypeAlias = arcllm.TraceStore
Usage = arcllm.Usage

Model: TypeAlias = arcllm.LLMProvider
"""Provider-neutral model protocol accepted by ArcRun."""

ModelTool: TypeAlias = arcllm.Tool
"""Tool schema sent to a model, distinct from ArcRun's executable ``Tool``."""

ModelModuleSetting: TypeAlias = bool | dict[str, Any] | None
ModelErrorKind: TypeAlias = Literal[
    "rate_limited", "content_filtered", "unavailable", "client_error", "provider_error"
]
ModelModuleKind: TypeAlias = Literal["circuit_breaker", "telemetry", "queue"]


@dataclass(frozen=True, slots=True)
class ModelAPIError:
    """Safe, provider-neutral details from a model API failure.

    Provider response bodies are intentionally not exposed: they can contain
    request fragments, credentials, or other deployment-specific details.
    """

    status_code: int
    provider: str
    retry_after: float | None
    kind: ModelErrorKind


def model_api_error(exc: BaseException) -> ModelAPIError | None:
    """Normalize an ArcLLM provider error without exposing its response body."""
    if not isinstance(exc, arcllm.ArcLLMAPIError):
        return None

    status = exc.status_code
    body = exc.body.lower()
    if status == 429:
        kind: ModelErrorKind = "rate_limited"
    elif status == 400 and "content_filter" in body:
        kind = "content_filtered"
    elif status in {500, 502, 503, 504}:
        kind = "unavailable"
    elif 400 <= status < 500:
        kind = "client_error"
    else:
        kind = "provider_error"
    return ModelAPIError(status, exc.provider, exc.retry_after, kind)


def validate_model_modules(modules: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of model-module overrides after validating their names."""
    result = dict(modules or {})
    unknown = set(result).difference(arcllm.MODULE_NAMES)
    if unknown:
        raise ValueError(
            f"Unknown model module key(s): {sorted(unknown)}. "
            f"Valid keys: {sorted(arcllm.MODULE_NAMES)}."
        )
    return result


def load_model(
    provider: str,
    model: str | None = None,
    *,
    budget_scope: str | None = None,
    on_event: Callable[[Any], None] | None = None,
    trace_store: TraceStore | None = None,
    agent_label: str | None = None,
    agent_did: str | None = None,
    lineage: dict[str, Any] | None = None,
    modules: dict[str, Any] | None = None,
) -> Model:
    """Load a long-lived provider-neutral model through ArcLLM.

    ``modules`` is the single validated extension point for ArcLLM's optional
    routing, resilience, security, and telemetry wrappers.
    """
    module_settings = validate_model_modules(modules)
    return arcllm.load_model(
        provider,
        model,
        budget_scope=budget_scope,
        on_event=on_event,
        trace_store=trace_store,
        agent_label=agent_label,
        agent_did=agent_did,
        lineage=lineage,
        **module_settings,
    )


def model_provider_keys() -> tuple[ProviderKey, ...]:
    """Return the canonical, immutable catalog of packaged provider keys."""
    return arcllm.list_provider_keys()


def model_config_path() -> Path:
    """Return the canonical model-runtime configuration file."""
    return arcllm.model_config_path()


def iter_model_modules(instance: Any) -> Iterator[tuple[ModelModuleKind, Any]]:
    """Yield ArcRun-observable model modules without exposing ArcLLM to callers."""
    current = instance
    while current is not None:
        if isinstance(current, arcllm.CircuitBreakerModule):
            yield "circuit_breaker", current
        if isinstance(current, arcllm.TelemetryModule):
            yield "telemetry", current
        if isinstance(current, arcllm.QueueModule):
            yield "queue", current
        current = getattr(current, "_inner", None)


def create_model_trace_store(
    agent_root: Path,
    *,
    retention_max_age_days: int | None = None,
    retention_max_bytes: int | None = None,
    checkpoint_sink: Callable[[dict[str, Any]], None] | None = None,
) -> TraceStore:
    """Construct ArcRun's persistent model-call trace store."""
    return arcllm.JSONLTraceStore(
        agent_root,
        retention_max_age_days=retention_max_age_days,
        retention_max_bytes=retention_max_bytes,
        checkpoint_sink=checkpoint_sink,
    )


def model_identity(
    agent_did: str | None, agent_label: str | None = None
) -> AbstractContextManager[None]:
    """Bind model trace identity to the current task for the context's lifetime."""
    return arcllm.agent_identity(agent_did, agent_label)


__all__ = [
    "ContentBlock",
    "Delta",
    "ImageBlock",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "Model",
    "ModelAPIError",
    "ModelErrorKind",
    "ModelModuleKind",
    "ModelModuleSetting",
    "ModelTool",
    "ProviderKey",
    "ResponseFormat",
    "StopReason",
    "TextBlock",
    "ToolCall",
    "ToolCallDelta",
    "ToolResultBlock",
    "ToolUseBlock",
    "TraceStore",
    "Usage",
    "create_model_trace_store",
    "iter_model_modules",
    "load_model",
    "model_api_error",
    "model_config_path",
    "model_identity",
    "model_provider_keys",
    "validate_model_modules",
]
