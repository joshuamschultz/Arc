"""Shared call-budget + telemetry plumbing for ``embed()`` and ``classify()``.

SPEC-038 budget accounting (pre-flight estimate, cumulative limits, the
standard ``ArcLLMBudgetError`` breach) and the one ``llm_call`` telemetry
record per call are identical across both seams — this module is the single
public home for that shared behavior so neither seam owns the other's
internals (mirrors ``telemetry_cost.calculate_cost``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arcllm.modules.base import resolve_enforcement
from arcllm.modules.telemetry_budget import (
    BudgetAccumulator,
    get_or_create_accumulator,
    validate_budget_scope,
)
from arcllm.types import Usage

if TYPE_CHECKING:  # keep arcstore off the module-import hot path
    from arcstore.records import SpoolRecord

logger = logging.getLogger(__name__)


def count_tokens(texts: list[str]) -> int:
    """Cheap, deterministic input-token estimate for budget accounting.

    Whitespace-word count (at least 1 per non-empty text). No tokenizer
    download — the estimate only feeds cost arithmetic, not the model.
    """
    return sum(max(1, len(t.split())) for t in texts)


@dataclass(frozen=True)
class BudgetPlan:
    scope: str
    accumulator: BudgetAccumulator
    monthly_limit: float | None
    daily_limit: float | None
    per_call_max: float | None
    enforcement: str


def parse_budget(telemetry: dict[str, Any]) -> BudgetPlan | None:
    """Build a budget plan from the telemetry config, or ``None`` when no scope
    is set. Spend is tracked whenever a ``budget_scope`` is present (call spend
    aggregates onto the agent's shared budget); limits enforce only when set."""
    scope = telemetry.get("budget_scope")
    if not scope:
        return None
    validate_budget_scope(scope)
    return BudgetPlan(
        scope=scope,
        accumulator=get_or_create_accumulator(scope),
        monthly_limit=telemetry.get("monthly_limit_usd"),
        daily_limit=telemetry.get("daily_limit_usd"),
        per_call_max=telemetry.get("per_call_max_usd"),
        enforcement=resolve_enforcement(telemetry, default="block"),
    )


def _breach(
    plan: BudgetPlan,
    limit_type: str,
    limit_usd: float,
    current: float,
    estimated: float | None,
    *,
    call_kind: str,
) -> None:
    """Enforce one limit: raise the standard breach (block) or warn.

    ``call_kind`` (``"embed"``, ``"classify"``, …) names the caller in the
    warn-mode log line so an operator can tell which seam is over budget.
    """
    if plan.enforcement == "block":
        from arcllm.exceptions import ArcLLMBudgetError

        raise ArcLLMBudgetError(
            scope=plan.scope,
            limit_type=limit_type,
            limit_usd=limit_usd,
            current_usd=current,
            estimated_usd=estimated,
        )
    logger.warning(
        "%s budget %s limit exceeded for '%s' (warn mode): limit=$%.6f",
        call_kind,
        limit_type,
        plan.scope,
        limit_usd,
    )


def budget_pre_check(
    plan: BudgetPlan, pre_tokens: int, cost_input_per_1m: float, *, call_kind: str
) -> None:
    """Pre-flight per-call estimate + cumulative check, before the call runs."""
    if plan.per_call_max is not None:
        estimated = pre_tokens * cost_input_per_1m / 1_000_000
        if estimated > plan.per_call_max:
            _breach(
                plan,
                "per_call",
                plan.per_call_max,
                plan.accumulator.monthly_spend,
                estimated,
                call_kind=call_kind,
            )

    monthly = plan.monthly_limit if plan.monthly_limit is not None else float("inf")
    daily = plan.daily_limit if plan.daily_limit is not None else float("inf")
    exceeded = plan.accumulator.check_limits(monthly, daily)
    if exceeded == "monthly" and plan.monthly_limit is not None:
        _breach(
            plan,
            "monthly",
            plan.monthly_limit,
            plan.accumulator.monthly_spend,
            None,
            call_kind=call_kind,
        )
    elif exceeded == "daily" and plan.daily_limit is not None:
        _breach(
            plan,
            "daily",
            plan.daily_limit,
            plan.accumulator.daily_spend,
            None,
            call_kind=call_kind,
        )


def emit_telemetry(
    telemetry: dict[str, Any],
    on_event: Callable[[SpoolRecord], None] | None,
    *,
    provider_label: str,
    model: str,
    usage: Usage,
    cost: float,
    latency_ms: float,
    operation: str,
    request_body: dict[str, Any] | None = None,
    response_body: dict[str, Any] | None = None,
) -> None:
    """Emit an ``llm_call`` telemetry record for one embed/classify call.

    Request/response bodies ride ``extra`` so the trace UI shows the actual
    call, not ``null``. ``operation`` is stamped as a first-class ``extra``
    key so the trace can be filtered by what the call was for.
    """
    from arcstore.records import SpoolRecord
    from arcstore.spool import record as spool_record

    extra: dict[str, Any] = {"operation": operation}
    if request_body is not None:
        extra["request_body"] = request_body
    if response_body is not None:
        extra["response_body"] = response_body
    event = SpoolRecord(
        kind="llm_call",
        actor_did=telemetry.get("agent_did") or "did:arc:unknown",
        model=model,
        provider=provider_label,
        agent_label=telemetry.get("agent_label"),
        prompt_tokens=usage.input_tokens,
        completion_tokens=0,
        cost_usd=cost,
        latency_ms=latency_ms,
        outcome="ok",
        extra=extra,
    )
    if on_event is not None:
        on_event(event)
    if telemetry.get("arcstore_enabled", True):
        spool_record(event)
