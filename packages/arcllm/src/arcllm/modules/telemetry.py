"""TelemetryModule — structured logging of timing, tokens, and cost per invoke().

Budget enforcement (when configured) integrates pre-check before the LLM call
and post-deduct after response. Per-scope accumulators live in
``telemetry_budget``; cost arithmetic lives in ``telemetry_cost``; this module
wires both into the OTel-instrumented ``invoke()`` flow.
"""

import logging
import threading
import time
from typing import Any, Literal

from arcstore.records import SpoolRecord as _SpoolRecord
from arcstore.spool import record as _spool_record
from opentelemetry import trace

from arcllm.exceptions import ArcLLMBudgetError, ArcLLMConfigError
from arcllm.modules._logging import log_structured, validate_log_level
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.modules.telemetry_budget import (
    BudgetAccumulator,
    clear_budgets,  # noqa: F401  re-exported for callers
    get_or_create_accumulator,
    validate_budget_scope,
)
from arcllm.modules.telemetry_cost import DEFAULT_MAX_TOKENS, calculate_cost, estimate_cost
from arcllm.trace_store import TraceRecord
from arcllm.types import LLMProvider, LLMResponse, Message, Tool, Usage

logger = logging.getLogger(__name__)

_UNKNOWN_DID = "did:arc:unknown"

_VALID_CONFIG_KEYS = {
    "cost_input_per_1m",
    "cost_output_per_1m",
    "cost_cache_read_per_1m",
    "cost_cache_write_per_1m",
    "log_level",
    "enabled",
    # Budget fields (all optional — budget disabled if none present)
    "monthly_limit_usd",
    "daily_limit_usd",
    "per_call_max_usd",
    "alert_threshold_pct",
    "enforcement",
    "budget_scope",
    "default_max_tokens",
    # Trace recording (all optional — disabled if none present)
    "on_event",
    "trace_store",
    "agent_label",
    "agent_did",
    "store_raw_bodies",
    # arcstore operational spool (SPEC-026 FR-4) — on by default.
    "arcstore_enabled",
}


# ---------------------------------------------------------------------------
# Global telemetry defaults — ensures every TelemetryModule instance
# routes traces to the shared trace_store and on_event callback.
# Set once via set_global_defaults() during UI/app startup.
# ---------------------------------------------------------------------------

_global_defaults_lock = threading.Lock()
_global_defaults: dict[str, Any] = {}


def set_global_defaults(
    *,
    on_event: Any = None,
    trace_store: Any = None,
    agent_did: str | None = None,
) -> None:
    """Set global defaults for all TelemetryModule instances.

    All TelemetryModule instances created after this call will use
    these defaults unless explicitly overridden in their config.
    Existing instances are NOT retroactively updated.

    Args:
        on_event: Callback fired after every invoke() with a TraceRecord.
        trace_store: TraceStore for persistent recording.
        agent_did: Agent DID for trace attribution.
    """
    with _global_defaults_lock:
        _global_defaults.clear()
        if on_event is not None:
            _global_defaults["on_event"] = on_event
        if trace_store is not None:
            _global_defaults["trace_store"] = trace_store
        if agent_did is not None:
            _global_defaults["agent_did"] = agent_did


def clear_global_defaults() -> None:
    """Clear all global defaults. Use in tests for isolation."""
    with _global_defaults_lock:
        _global_defaults.clear()


# ---------------------------------------------------------------------------
# TelemetryModule
# ---------------------------------------------------------------------------


class TelemetryModule(BaseModule):
    """Wraps invoke() to log timing, token usage, and cost.

    When budget fields are present in config, also enforces spend limits:
    pre-check before the LLM call, post-deduct after response.

    Config keys:
        cost_input_per_1m: Cost per 1M input tokens (default: 0.0).
        cost_output_per_1m: Cost per 1M output tokens (default: 0.0).
        cost_cache_read_per_1m: Cost per 1M cache read tokens (default: 0.0).
        cost_cache_write_per_1m: Cost per 1M cache write tokens (default: 0.0).
        log_level: Python log level name (default: "INFO").
        monthly_limit_usd: Monthly spend limit (optional).
        daily_limit_usd: Daily spend limit (optional).
        per_call_max_usd: Per-call cost ceiling (optional).
        alert_threshold_pct: Alert at this % of monthly limit (default: 80).
        enforcement: "block" or "warn" (default: "block").
        budget_scope: Required when budget is enabled.
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        validate_config_keys(config, _VALID_CONFIG_KEYS, "TelemetryModule")

        _cost_fields = (
            "cost_input_per_1m",
            "cost_output_per_1m",
            "cost_cache_read_per_1m",
            "cost_cache_write_per_1m",
        )
        for field in _cost_fields:
            if config.get(field, 0.0) < 0:
                raise ArcLLMConfigError(f"{field} must be >= 0")

        self._cost_input: float = config.get("cost_input_per_1m", 0.0)
        self._cost_output: float = config.get("cost_output_per_1m", 0.0)
        self._cost_cache_read: float = config.get("cost_cache_read_per_1m", 0.0)
        self._cost_cache_write: float = config.get("cost_cache_write_per_1m", 0.0)

        self._log_level: int = validate_log_level(config)

        # Budget config (all optional — budget disabled if no limits present)
        self._monthly_limit: float | None = config.get("monthly_limit_usd")
        self._daily_limit: float | None = config.get("daily_limit_usd")
        self._per_call_max: float | None = config.get("per_call_max_usd")
        self._alert_pct: float = config.get("alert_threshold_pct", 80)
        self._enforcement: str = config.get("enforcement", "block")
        self._budget_scope: str | None = config.get("budget_scope")
        self._default_max_tokens: int = config.get("default_max_tokens", DEFAULT_MAX_TOKENS)

        # Trace recording config (explicit config > global defaults)
        self._on_event: Any | None = config.get("on_event") or _global_defaults.get("on_event")
        self._trace_store: Any | None = config.get("trace_store") or _global_defaults.get(
            "trace_store"
        )
        self._agent_label: str | None = config.get("agent_label")
        self._agent_did: str | None = config.get("agent_did") or _global_defaults.get("agent_did")
        # Metadata-only by default (SPEC-026 FR-4 / C2): durable plaintext
        # prompts/responses are an exfiltration target (LLM02) and leak system
        # prompts (LLM07). Raw capture is an explicit, audited opt-in.
        self._store_raw_bodies: bool = config.get("store_raw_bodies", False)
        if self._store_raw_bodies:
            logger.warning(
                "store_raw_bodies=True: raw prompt/response bodies will be persisted — "
                "not safe for federal/CUI sessions (SPEC-026 FR-4)"
            )
        # arcstore operational spool recording — on by default (SPEC-026 FR-4).
        self._arcstore_enabled: bool = config.get("arcstore_enabled", True)

        self._budget_enabled = any(
            v is not None for v in (self._monthly_limit, self._daily_limit, self._per_call_max)
        )

        if self._budget_enabled:
            if self._enforcement not in ("warn", "block"):
                raise ArcLLMConfigError(
                    f"enforcement must be 'warn' or 'block', got '{self._enforcement}'"
                )
            for limit_name in ("monthly_limit_usd", "daily_limit_usd", "per_call_max_usd"):
                val = config.get(limit_name)
                if val is not None and val < 0:
                    raise ArcLLMConfigError(f"{limit_name} must be >= 0")
            if not (0 < self._alert_pct <= 100):
                raise ArcLLMConfigError(
                    f"alert_threshold_pct must be >0 and <=100, got {self._alert_pct}"
                )
            if not self._budget_scope:
                raise ArcLLMConfigError(
                    "budget_scope is required when budget limits are configured"
                )
            validate_budget_scope(self._budget_scope)
            self._accumulator: BudgetAccumulator = get_or_create_accumulator(self._budget_scope)

    def _calculate_cost(self, usage: Usage) -> float:
        """Calculate USD cost from token counts and per-1M pricing."""
        return calculate_cost(
            usage,
            input_per_1m=self._cost_input,
            output_per_1m=self._cost_output,
            cache_read_per_1m=self._cost_cache_read,
            cache_write_per_1m=self._cost_cache_write,
        )

    def _estimate_cost(self, max_tokens: int) -> float:
        """Estimate worst-case cost using max_tokens * output price."""
        return estimate_cost(max_tokens, output_per_1m=self._cost_output)

    def _enforce_limit(
        self,
        span: trace.Span,
        scope: str,
        limit_type: str,
        limit_usd: float,
        current_usd: float,
        estimated_usd: float | None,
        budget_meta: dict[str, Any],
    ) -> None:
        """Apply block-or-warn enforcement for a single limit violation."""
        if self._enforcement == "block":
            raise ArcLLMBudgetError(
                scope=scope,
                limit_type=limit_type,
                limit_usd=limit_usd,
                current_usd=current_usd,
                estimated_usd=estimated_usd,
            )
        budget_meta["budget_warning"] = True
        attrs: dict[str, Any] = {
            "scope": scope,
            "limit_type": limit_type,
            "limit_usd": limit_usd,
        }
        if estimated_usd is not None:
            attrs["estimated_usd"] = estimated_usd
        else:
            attrs["current_usd"] = current_usd
        span.add_event("budget_exceeded", attrs)

    def _check_budget_pre_call(self, span: trace.Span, **kwargs: Any) -> dict[str, Any] | None:
        """Run budget pre-flight checks. Returns warning metadata or raises.

        Returns None if no warning, or dict to merge into response metadata.
        """
        if not self._budget_enabled:
            return None

        scope = self._budget_scope
        if scope is None:  # Guaranteed by __init__ validation; defensive guard
            return None

        budget_meta: dict[str, Any] = {}

        # Pre-flight estimate check
        if self._per_call_max is not None:
            max_tokens = kwargs.get("max_tokens", self._default_max_tokens)
            estimated = self._estimate_cost(max_tokens)
            if self._accumulator.check_pre_flight(estimated, self._per_call_max):
                self._enforce_limit(
                    span,
                    scope,
                    "per_call",
                    self._per_call_max,
                    self._accumulator.monthly_spend,
                    estimated,
                    budget_meta,
                )

        # Cumulative limit check
        monthly_limit = self._monthly_limit or float("inf")
        daily_limit = self._daily_limit or float("inf")
        exceeded = self._accumulator.check_limits(monthly_limit, daily_limit)
        if exceeded is not None:
            limit_usd = self._monthly_limit if exceeded == "monthly" else self._daily_limit
            if limit_usd is None:  # Should not happen; defensive guard
                return None
            current = (
                self._accumulator.monthly_spend
                if exceeded == "monthly"
                else self._accumulator.daily_spend
            )
            self._enforce_limit(
                span,
                scope,
                exceeded,
                limit_usd,
                current,
                None,
                budget_meta,
            )

        # Alert threshold check (warning only, never blocks)
        if self._monthly_limit is not None:
            threshold = self._monthly_limit * self._alert_pct / 100
            if self._accumulator.monthly_spend >= threshold:
                span.add_event(
                    "budget_alert",
                    {
                        "scope": scope,
                        "monthly_spend_usd": self._accumulator.monthly_spend,
                        "monthly_limit_usd": self._monthly_limit,
                        "threshold_pct": self._alert_pct,
                    },
                )

        return budget_meta or None

    def _set_budget_otel(self, span: trace.Span, action: str) -> None:
        """Set budget-related OTel span attributes."""
        if not self._budget_enabled:
            return
        span.set_attribute("arcllm.budget.scope", self._budget_scope or "")
        span.set_attribute("arcllm.budget.enforcement", self._enforcement)
        span.set_attribute("arcllm.budget.monthly_spend_usd", self._accumulator.monthly_spend)
        span.set_attribute("arcllm.budget.daily_spend_usd", self._accumulator.daily_spend)
        if self._monthly_limit is not None:
            span.set_attribute("arcllm.budget.monthly_limit_usd", self._monthly_limit)
        if self._daily_limit is not None:
            span.set_attribute("arcllm.budget.daily_limit_usd", self._daily_limit)
        if self._per_call_max is not None:
            span.set_attribute("arcllm.budget.per_call_max_usd", self._per_call_max)
        span.set_attribute("arcllm.budget.action", action)

    def get_budget_state(self) -> dict[str, Any] | None:
        """Return current budget state for REST API queries.

        Returns None if budget is not enabled.
        """
        if not self._budget_enabled:
            return None
        return {
            "scope": self._budget_scope,
            "monthly_spend": self._accumulator.monthly_spend,
            "daily_spend": self._accumulator.daily_spend,
            "monthly_limit": self._monthly_limit,
            "daily_limit": self._daily_limit,
            "per_call_max": self._per_call_max,
            "enforcement": self._enforcement,
            "alert_threshold_pct": self._alert_pct,
        }

    def _build_trace_record(
        self,
        response: LLMResponse,
        cost: float,
        phase_timings: dict[str, float],
        messages: list[Message],
        tools: list[Tool] | None,
        kwargs: dict[str, Any],
        status: Literal["success", "error", "timeout"] = "success",
        error: str | None = None,
    ) -> TraceRecord:
        """Build a TraceRecord from invoke() data."""
        request_body: dict[str, Any] | None = None
        response_body: dict[str, Any] | None = None

        # Internal keys injected by upstream modules (RetryModule, QueueModule)
        _internal_keys = {"_retry_attempt", "_retry_group_id", "_queue_wait_ms"}

        if self._store_raw_bodies:
            request_body = {
                "messages": [m.model_dump() for m in messages],
                "tools": [t.model_dump() for t in tools] if tools else None,
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k != "max_tokens" and k not in _internal_keys
                },
            }
            if kwargs.get("max_tokens") is not None:
                request_body["max_tokens"] = kwargs["max_tokens"]

            response_body = {
                "content": response.content,
                "tool_calls": [tc.model_dump() for tc in response.tool_calls],
                "stop_reason": response.stop_reason,
            }

        # Extract retry metadata injected by RetryModule
        attempt_number: int = kwargs.get("_retry_attempt", 0)
        retry_group_id: str | None = kwargs.get("_retry_group_id")

        usage = response.usage
        return TraceRecord(
            provider=self._inner.name,
            model=response.model,
            agent_label=self._agent_label,
            agent_did=self._agent_did,
            budget_scope=self._budget_scope,
            request_body=request_body,
            response_body=response_body,
            duration_ms=phase_timings.get("total_ms", 0.0),
            cost_usd=cost,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            stop_reason=response.stop_reason,
            status=status,
            error=error,
            attempt_number=attempt_number,
            retry_group_id=retry_group_id,
            phase_timings=phase_timings,
        )

    async def _emit_trace(self, record: TraceRecord) -> None:
        """Append to trace_store and fire on_event callback (outside locks)."""
        if self._trace_store is not None:
            await self._trace_store.append(record)
        if self._on_event is not None:
            self._on_event(record)

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.telemetry") as tel_span:
            t0 = time.monotonic()
            try:
                response, cost, total_ms = await self._invoke_inner(
                    messages, tools, tel_span, t0, **kwargs
                )
            except Exception:
                # FR-4 / C3 — a raising call still records an operational line.
                self._record_spool(
                    outcome="error",
                    model=None,
                    cost=None,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                )
                raise
            self._record_spool(
                outcome="ok",
                model=response.model,
                cost=cost,
                latency_ms=total_ms,
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
            )
            return response

    async def _invoke_inner(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        tel_span: trace.Span,
        t0: float,
        **kwargs: Any,
    ) -> tuple[LLMResponse, float, float]:
        # Budget pre-check (before calling inner provider)
        budget_meta = self._check_budget_pre_call(tel_span, **kwargs)

        # Strip internal metadata keys before forwarding to inner provider
        inner_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}

        t_pre = time.monotonic()
        response = await self._inner.invoke(messages, tools, **inner_kwargs)
        t_llm = time.monotonic()

        usage = response.usage
        cost = self._calculate_cost(usage)

        # Budget post-deduct (after successful call)
        if self._budget_enabled:
            safe_cost = max(0.0, cost)
            self._accumulator.deduct(safe_cost)
            action = "warned" if budget_meta else "allowed"
            self._set_budget_otel(tel_span, action)

        # Merge budget metadata into response
        updates: dict[str, Any] = {"cost_usd": cost}
        if budget_meta:
            existing_meta = response.metadata or {}
            updates["metadata"] = {**existing_meta, **budget_meta}
        response = response.model_copy(update=updates)

        t_post = time.monotonic()

        prompt_assembly_ms = round((t_pre - t0) * 1000, 1)
        llm_call_ms = round((t_llm - t_pre) * 1000, 1)
        post_processing_ms = round((t_post - t_llm) * 1000, 1)
        total_ms = round((t_post - t0) * 1000, 1)

        phase_timings = {
            "prompt_assembly_ms": prompt_assembly_ms,
            "llm_call_ms": llm_call_ms,
            "post_processing_ms": post_processing_ms,
            "total_ms": total_ms,
        }

        tel_span.set_attribute("arcllm.telemetry.duration_ms", total_ms)
        tel_span.set_attribute("arcllm.telemetry.cost_usd", cost)

        log_structured(
            logger,
            self._log_level,
            "LLM call",
            provider=self._inner.name,
            model=response.model,
            duration_ms=total_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=cost,
            stop_reason=response.stop_reason,
        )

        # Build and emit trace record (fire-and-forget for trace_store)
        if self._trace_store is not None or self._on_event is not None:
            record = self._build_trace_record(
                response,
                cost,
                phase_timings,
                messages,
                tools,
                kwargs,
            )
            await self._emit_trace(record)

        return response, cost, total_ms

    def _record_spool(
        self,
        *,
        outcome: str,
        model: str | None,
        cost: float | None,
        latency_ms: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Append one ``llm_call`` operational record to the arcstore spool.

        On by default (``arcstore_enabled``); imports only ``arcstore.spool``.
        ``record()`` is itself fail-open, so this never breaks the call.
        """
        if not self._arcstore_enabled:
            return
        _spool_record(
            _SpoolRecord(
                kind="llm_call",
                actor_did=self._agent_did or _UNKNOWN_DID,
                model=model,
                provider=self._inner.name,
                agent_label=self._agent_label,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                outcome=outcome,
            )
        )


# Test/legacy import aliases for callers that imported the budget helpers
# from this module before the Phase 5 §8.11 split.
_validate_budget_scope = validate_budget_scope
_get_or_create_accumulator = get_or_create_accumulator
