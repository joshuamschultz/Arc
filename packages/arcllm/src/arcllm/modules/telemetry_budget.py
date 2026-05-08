"""Budget accumulator + per-scope registry for TelemetryModule.

Tracks monthly and daily spend per scope with calendar-period auto-reset.
The accumulators are shared across all TelemetryModule instances that
configure the same ``budget_scope`` so spend is aggregated, not isolated.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import UTC, datetime

from arcllm.exceptions import ArcLLMConfigError

_BUDGET_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_:.\-]{0,127}$")


def utc_month_key() -> int:
    """Return current UTC month as YYYYMM integer."""
    now = datetime.now(UTC)
    return now.year * 100 + now.month


def utc_day_key() -> int:
    """Return current UTC day as YYYYMMDD integer."""
    now = datetime.now(UTC)
    return now.year * 10000 + now.month * 100 + now.day


def validate_budget_scope(scope: str) -> None:
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
        self._current_month: int = utc_month_key()
        self._current_day: int = utc_day_key()

    def _maybe_reset(self) -> None:
        """Reset accumulators if the calendar period has changed.

        Caller must hold ``self._lock``.
        """
        month = utc_month_key()
        day = utc_day_key()
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


_budget_lock = threading.Lock()
_budget_registry: dict[str, BudgetAccumulator] = {}


def get_or_create_accumulator(scope: str) -> BudgetAccumulator:
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
