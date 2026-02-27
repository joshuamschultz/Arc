"""TelemetryModule — structured logging of timing, tokens, and cost per invoke().

Includes BudgetAccumulator for per-scope spend tracking with calendar period
resets. Budget enforcement is integrated into the telemetry invoke() flow:
pre-check before the LLM call, post-deduct after response.
"""

import logging
import re
import threading
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from arcllm.exceptions import ArcLLMBudgetError, ArcLLMConfigError
from arcllm.modules._logging import log_structured, validate_log_level
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.types import LLMProvider, LLMResponse, Message, Tool, Usage

logger = logging.getLogger(__name__)

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
}

# Fallback for pre-flight cost estimation when max_tokens not passed by caller
_DEFAULT_MAX_TOKENS = 4096

_BUDGET_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_:.\-]{0,127}$")


# ---------------------------------------------------------------------------
# UTC period helpers (module-level for testability via mock)
# ---------------------------------------------------------------------------


def _utc_month_key() -> int:
    """Return current UTC month as YYYYMM integer."""
    now = datetime.now(UTC)
    return now.year * 100 + now.month


def _utc_day_key() -> int:
    """Return current UTC day as YYYYMMDD integer."""
    now = datetime.now(UTC)
    return now.year * 10000 + now.month * 100 + now.day


# ---------------------------------------------------------------------------
# Budget scope validation
# ---------------------------------------------------------------------------


def _validate_budget_scope(scope: str) -> None:
    """Validate budget scope string for safety.

    NFKC normalization prevents Unicode homoglyph attacks.
    Regex restricts to lowercase alphanumeric + colons, dots, hyphens.
    Max 128 characters.

    Raises:
        ArcLLMConfigError: On invalid scope string.
    """
    if not scope:
        raise ArcLLMConfigError("budget_scope cannot be empty")
    normalized = unicodedata.normalize("NFKC", scope)
    if normalized != scope or not _BUDGET_SCOPE_RE.match(scope):
        raise ArcLLMConfigError(
            f"Invalid budget_scope '{scope}'. Must be lowercase alphanumeric "
            "with underscores, colons, dots, or hyphens, max 128 characters."
        )


# ---------------------------------------------------------------------------
# BudgetAccumulator — per-scope spend tracker
# ---------------------------------------------------------------------------


class BudgetAccumulator:
    """Per-scope spend tracker with calendar period resets.

    Tracks monthly and daily spend. Automatically resets when the UTC
    calendar period changes. Costs are clamped to max(0.0, cost) to
    prevent negative cost injection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.monthly_spend: float = 0.0
        self.daily_spend: float = 0.0
        self._current_month: int = _utc_month_key()
        self._current_day: int = _utc_day_key()

    def _maybe_reset(self) -> None:
        """Reset accumulators if the calendar period has changed.

        Caller must hold ``self._lock``.
        """
        month = _utc_month_key()
        day = _utc_day_key()
        if month != self._current_month:
            self.monthly_spend = 0.0
            self.daily_spend = 0.0
            self._current_month = month
            self._current_day = day
        elif day != self._current_day:
            self.daily_spend = 0.0
            self._current_day = day

    def deduct(self, cost: float) -> None:
        """Add clamped cost to both accumulators after period check."""
        with self._lock:
            self._maybe_reset()
            safe_cost = max(0.0, cost)
            self.monthly_spend += safe_cost
            self.daily_spend += safe_cost

    def check_limits(self, monthly_limit: float, daily_limit: float) -> str | None:
        """Return exceeded limit type or None if within bounds.

        Checks monthly first (takes priority).
        """
        with self._lock:
            self._maybe_reset()
            if self.monthly_spend >= monthly_limit:
                return "monthly"
            if self.daily_spend >= daily_limit:
                return "daily"
            return None

    def check_pre_flight(self, estimated: float, per_call_max: float) -> bool:
        """Return True if estimated cost exceeds per-call max."""
        return estimated > per_call_max


# ---------------------------------------------------------------------------
# Per-scope budget registry (shared state, like _bucket_registry)
# ---------------------------------------------------------------------------

_budget_lock = threading.Lock()
_budget_registry: dict[str, BudgetAccumulator] = {}


def _get_or_create_accumulator(scope: str) -> BudgetAccumulator:
    """Return the shared accumulator for *scope*, creating one if needed.

    Uses double-check locking for PEP 703 (free-threading) readiness.
    """
    if scope not in _budget_registry:
        with _budget_lock:
            if scope not in _budget_registry:
                _budget_registry[scope] = BudgetAccumulator()
    return _budget_registry[scope]


def clear_budgets() -> None:
    """Remove all shared accumulators (for test isolation and cache resets)."""
    with _budget_lock:
        _budget_registry.clear()


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
        self._default_max_tokens: int = config.get("default_max_tokens", _DEFAULT_MAX_TOKENS)

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
            _validate_budget_scope(self._budget_scope)
            self._accumulator: BudgetAccumulator = _get_or_create_accumulator(self._budget_scope)

    def _calculate_cost(self, usage: Usage) -> float:
        """Calculate USD cost from token counts and per-1M pricing."""
        cost = (
            usage.input_tokens * self._cost_input + usage.output_tokens * self._cost_output
        ) / 1_000_000

        if usage.cache_read_tokens:
            cost += usage.cache_read_tokens * self._cost_cache_read / 1_000_000
        if usage.cache_write_tokens:
            cost += usage.cache_write_tokens * self._cost_cache_write / 1_000_000

        return cost

    def _estimate_cost(self, max_tokens: int) -> float:
        """Estimate worst-case cost using max_tokens * output price."""
        return max_tokens * self._cost_output / 1_000_000

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

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.telemetry") as tel_span:
            # Budget pre-check (before calling inner provider)
            budget_meta = self._check_budget_pre_call(tel_span, **kwargs)

            start = time.monotonic()
            response = await self._inner.invoke(messages, tools, **kwargs)
            elapsed = time.monotonic() - start

            usage = response.usage
            cost = self._calculate_cost(usage)
            duration_ms = round(elapsed * 1000, 1)

            tel_span.set_attribute("arcllm.telemetry.duration_ms", duration_ms)
            tel_span.set_attribute("arcllm.telemetry.cost_usd", cost)

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

            log_structured(
                logger,
                self._log_level,
                "LLM call",
                provider=self._inner.name,
                model=response.model,
                duration_ms=duration_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=cost,
                stop_reason=response.stop_reason,
            )

            return response
