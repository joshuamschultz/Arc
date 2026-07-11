"""TelemetryModule — structured logging of timing, tokens, and cost per invoke().

Budget enforcement (when configured) integrates pre-check before the LLM call
and post-deduct after response. Per-scope accumulators live in
``telemetry_budget``; cost arithmetic lives in ``telemetry_cost``; this module
wires both into the OTel-instrumented ``invoke()`` flow.
"""

import contextlib
import contextvars
import json
import logging
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from arcstore.records import SpoolRecord as _SpoolRecord
from arcstore.spool import record as _spool_record
from arctrust.fips import assert_fips_if_required
from opentelemetry import trace
from pydantic import ValidationError

from arcllm._trace_crypto import decode_wrapping_key, seal
from arcllm.config import TraceEncryptionConfig
from arcllm.exceptions import ArcLLMBudgetError, ArcLLMConfigError
from arcllm.modules._logging import log_structured, validate_log_level
from arcllm.modules.base import BaseModule, resolve_enforcement, validate_config_keys
from arcllm.modules.telemetry_budget import (
    BudgetAccumulator,
    get_or_create_accumulator,
    validate_budget_scope,
)
from arcllm.modules.telemetry_budget import (
    clear_budgets as clear_budgets,
)
from arcllm.modules.telemetry_cost import DEFAULT_MAX_TOKENS, calculate_cost, estimate_cost
from arcllm.trace_store import EncryptedEnvelope, TraceRecord
from arcllm.types import LLMProvider, LLMResponse, Message, Tool, Usage

logger = logging.getLogger(__name__)

_UNKNOWN_DID = "did:arc:unknown"

# Per-body size cap (FR-23): a raw body over this size is replaced with a
# truncation marker rather than writing an unbounded JSONL line. 256KB
# comfortably covers a large single-turn prompt while bounding the
# write-amplification a 100k-token context would otherwise cause at scale.
_DEFAULT_MAX_BODY_BYTES = 256 * 1024

# lineage/classification are attacker-influenceable, persisted-verbatim
# metadata (D-443) — capped so a homoglyph or oversized blob can't poison
# a downstream log viewer or classification filter (LLM01 hardening).
_MAX_CLASSIFICATION_LEN = 128
_MAX_LINEAGE_BYTES = 8192

# Classification ordering floor enforcement (D-439). Unrecognized labels
# rank below every configured floor — fail-safe: an unverifiable label
# can never silently satisfy or exceed the floor.
_CLASSIFICATION_RANK: dict[str, int] = {
    "unclassified": 0,
    "cui": 1,
    "confidential": 2,
    "secret": 3,
    "top_secret": 4,
}

# kwargs injected by upstream modules or SPEC-016 verbatim-persistence
# fields — never forwarded to the wrapped provider's invoke() and never
# folded into the captured request_body (they're recorded on the
# TraceRecord's own dedicated fields instead).
_INTERNAL_KWARG_KEYS = {
    "_retry_attempt",
    "_retry_group_id",
    "_queue_wait_ms",
    "lineage",
    "classification",
}

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
    "max_body_bytes",
    # SPEC-016 — classification watermark floor, lineage default, envelope
    # encryption. All resolved once at construction (AU-2: tier posture
    # must flow through construction, never re-read per call).
    "classification",
    "lineage",
    "encryption",
    "encryption_key_secret",
    # arcstore operational spool (SPEC-026 FR-4) — on by default.
    "arcstore_enabled",
}


def _classification_rank(level: str) -> int:
    """Ordinal rank for a classification level; unrecognized labels rank -1."""
    return _CLASSIFICATION_RANK.get(level.lower(), -1)


def _cap_str(value: str, max_len: int) -> str:
    """NFKC-normalize then length-cap a persisted, attacker-influenceable string."""
    normalized = unicodedata.normalize("NFKC", value)
    return normalized[:max_len]


def _text_len_hint(messages: list[Message]) -> int:
    """Lower-bound character count of a message list's text content (M4).

    A true lower bound on the eventual JSON-encoded byte size of the
    request body — JSON quoting/escaping/structure only ever ADDS bytes
    relative to the raw text it wraps, never removes them. Used to decide
    whether the full ``model_dump()`` + ``json.dumps()`` pass can be
    skipped because the result is already certain to exceed the cap.
    """
    total = 0
    for m in messages:
        if isinstance(m.content, str):
            total += len(m.content)
        elif isinstance(m.content, list):
            for block in m.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    total += len(text)
    return total


def _response_text_len_hint(response: LLMResponse) -> int:
    """Lower-bound character count of a response's text content (M4)."""
    total = len(response.content) if response.content else 0
    for tc in response.tool_calls:
        total += len(str(tc.arguments))
    return total


@dataclass(frozen=True)
class _PreparedBodies:
    """Request/response bodies + encryption envelope, built exactly once
    per invoke() outcome and shared by the trace_store record and the
    arcstore spool (H1/M4). ``request_body``/``response_body`` are both
    ``None`` when sealed into ``encryption`` — see ``_prepare_bodies``.
    """

    trace_id: str
    timestamp: str
    request_body: dict[str, Any] | None
    response_body: dict[str, Any] | None
    encryption: EncryptedEnvelope | None


def resolve_classification(requested: str | None, floor: str) -> str:
    """Resolve the per-record classification tag, never below ``floor``.

    ``requested`` arrives per-call (upstream, data-source-aware); ``floor``
    is the config-supplied tier minimum. An unrecognized or lower-ranked
    ``requested`` value never downgrades below the floor (D-439) —
    arcllm enforces the floor, it does not classify content.
    """
    if requested is None:
        return floor
    if _classification_rank(requested) >= _classification_rank(floor):
        return _cap_str(requested, _MAX_CLASSIFICATION_LEN)
    return floor


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
# Task-local agent identity (SPEC-028 C2) — contextvars, not a process global.
#
# A spawned child reuses its parent's model (and therefore its parent's
# TelemetryModule). To attribute the child's llm_call records to the *child*
# without rebuilding the provider chain, the child binds its identity on its own
# asyncio task via a ContextVar. ``asyncio.Task`` snapshots the context at
# creation, so concurrent ``spawn_many`` children each carry an independent copy
# — no cross-attribution (the race that ``set_global_defaults`` would cause).
# Resolution priority: ContextVar > module config > global defaults.
# ---------------------------------------------------------------------------

_agent_did_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arcllm_agent_did", default=None
)
_agent_label_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arcllm_agent_label", default=None
)


@contextlib.contextmanager
def agent_identity(agent_did: str | None, agent_label: str | None = None) -> Iterator[None]:
    """Bind the operational identity for llm_call records on this asyncio task.

    Task-local: set the binding *before* awaiting the child run so the child's
    LLM calls (same task) resolve to this identity, then reset on exit.
    """
    did_token = _agent_did_var.set(agent_did)
    label_token = _agent_label_var.set(agent_label)
    try:
        yield
    finally:
        _agent_did_var.reset(did_token)
        _agent_label_var.reset(label_token)


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
        # Full raw capture by default (SPEC-016 D-435): every call is
        # reconstructable for forensic replay unless an operator makes the
        # deliberate, audited choice to disable it (see _maybe_audit_disable).
        self._store_raw_bodies: bool = config.get("store_raw_bodies", True)
        self._audit_disable_pending = not self._store_raw_bodies
        if not self._store_raw_bodies:
            logger.warning(
                "store_raw_bodies=False: full request/response capture is disabled — "
                "this session will not be reconstructable for replay (SPEC-016 D-435)"
            )
        self._max_body_bytes: int = config.get("max_body_bytes", _DEFAULT_MAX_BODY_BYTES)

        # Classification watermark floor (SPEC-016 D-439) + lineage default
        # (D-443). Both resolved once here, at construction, from the
        # tier-derived config — never re-read per call (AU-2).
        self._classification_floor: str = _cap_str(
            config.get("classification", "unclassified"), _MAX_CLASSIFICATION_LEN
        )
        self._lineage_default: dict[str, Any] | None = config.get("lineage")

        # Envelope encryption (SPEC-016 D-438). Resolved once at construction;
        # the wrapping key is never re-resolved per call.
        try:
            self._encryption_config = TraceEncryptionConfig(**(config.get("encryption") or {}))
        except ValidationError as e:
            raise ArcLLMConfigError(f"Invalid telemetry encryption config: {e}") from e

        self._wrapping_key: bytes | None = None
        if self._encryption_config.enabled:
            # One arctrust gate covers signing AND encryption (SPEC-037). Trace
            # bodies are sealed with AES-256-GCM — a FIPS-approved algorithm.
            assert_fips_if_required(
                require_fips=self._encryption_config.require_fips,
                algorithm="aes-256-gcm",
            )
            secret = config.get("encryption_key_secret")
            if not secret:
                raise ArcLLMConfigError(
                    "modules.telemetry.encryption.enabled=true but no wrapping key "
                    "was resolved (missing encryption_key_secret) — fail-closed, "
                    "never falls back to plaintext capture"
                )
            self._wrapping_key = decode_wrapping_key(secret)

        # arcstore operational spool recording — on by default (SPEC-026 FR-4).
        self._arcstore_enabled: bool = config.get("arcstore_enabled", True)

        self._budget_enabled = any(
            v is not None for v in (self._monthly_limit, self._daily_limit, self._per_call_max)
        )

        if self._budget_enabled:
            self._enforcement = resolve_enforcement(config, default=self._enforcement)
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

    def _resolve_agent_did(self) -> str | None:
        """ContextVar identity (a spawned child) > config/global (C2). May be None."""
        return _agent_did_var.get() or self._agent_did

    def _resolve_agent_label(self) -> str | None:
        """ContextVar label (a spawned child) > config/global."""
        return _agent_label_var.get() or self._agent_label

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

    def _raw_bodies(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        kwargs: dict[str, Any],
        response: LLMResponse | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Build (request_body, response_body) when raw capture is enabled.

        Returns ``(None, None)`` when ``store_raw_bodies`` is off (the federal/
        CUI default). ``response_body`` is ``None`` when there is no response
        (the error path). Called exactly ONCE per invoke() outcome by
        ``_prepare_bodies`` (H1/M4) — never rebuilt independently for the
        trace_store record and the arcstore spool.
        """
        if not self._store_raw_bodies:
            return None, None
        return self._build_request_body(messages, tools, kwargs), self._build_response_body(
            response
        )

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the capped request body.

        A cheap lower-bound text-length hint is checked FIRST (M4): JSON
        encoding a message list only ever ADDS bytes (quoting, escaping,
        struct keys) relative to the raw text it carries, so if that raw
        text alone already exceeds ``max_body_bytes`` the full
        ``model_dump()`` + ``json.dumps()`` pass is guaranteed to exceed it
        too. Skipping straight to the truncation marker means a 100k-token
        context is never fully serialized just to immediately discard the
        result as oversized.
        """
        cheap_len = _text_len_hint(messages)
        if cheap_len > self._max_body_bytes:
            return {"truncated": True, "original_bytes": cheap_len}

        body: dict[str, Any] = {
            "messages": [m.model_dump() for m in messages],
            "tools": [t.model_dump() for t in tools] if tools else None,
            **{
                k: v
                for k, v in kwargs.items()
                if k != "max_tokens" and k not in _INTERNAL_KWARG_KEYS
            },
        }
        if kwargs.get("max_tokens") is not None:
            body["max_tokens"] = kwargs["max_tokens"]
        return self._cap_body(body, self._max_body_bytes)

    def _build_response_body(self, response: LLMResponse | None) -> dict[str, Any] | None:
        """Build the capped response body, or ``None`` on the error path.

        Same cheap-hint short-circuit as ``_build_request_body`` (M4).
        """
        if response is None:
            return None
        cheap_len = _response_text_len_hint(response)
        if cheap_len > self._max_body_bytes:
            return {"truncated": True, "original_bytes": cheap_len}

        body: dict[str, Any] = {
            "content": response.content,
            "tool_calls": [tc.model_dump() for tc in response.tool_calls],
            "stop_reason": response.stop_reason,
        }
        return self._cap_body(body, self._max_body_bytes)

    @staticmethod
    def _cap_body(body: dict[str, Any], max_bytes: int) -> dict[str, Any]:
        """Truncate a dict payload over ``max_bytes`` (FR-23).

        Shared by request/response body capping and lineage capping (a
        smaller cap — lineage is provenance metadata, not a document copy)
        — replace the oversized payload with a small marker rather than
        writing an unbounded JSONL line. Callers resolve ``None`` (no
        lineage, no response on the error path) before calling this —
        there is no body-shaped ``None`` this needs to pass through.
        """
        encoded = json.dumps(body, default=str).encode("utf-8")
        if len(encoded) <= max_bytes:
            return body
        return {"truncated": True, "original_bytes": len(encoded)}

    def _prepare_bodies(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        kwargs: dict[str, Any],
        response: LLMResponse | None,
    ) -> "_PreparedBodies":
        """Build request/response bodies exactly ONCE per invoke() outcome (H1).

        Shared by both the trace_store record and the arcstore operational
        spool. Previously each independently called ``_raw_bodies`` (M4 —
        double model_dump()+json.dumps() per call), and worse, the spool
        always received the UNSEALED plaintext bodies even when envelope
        encryption was enabled — a federal deployment with encryption on
        still wrote plaintext CUI to the operational spool.

        When encryption is enabled the bodies are sealed ONCE here into an
        ``EncryptedEnvelope`` and the plaintext dicts are discarded
        entirely — both consumers see the same ``(None, None, envelope)``
        result. The arcstore spool then simply omits raw bodies for that
        call (metadata-only); the encrypted trace_store record is the sole
        body-of-record. This is the simpler of the two documented options
        (duplicate the ciphertext into the spool vs. omit it there) — one
        body-of-record, never two copies of the same envelope.
        """
        # Generated explicitly (rather than left to TraceRecord's
        # default_factory) so envelope sealing can bind the SAME trace_id
        # + timestamp into the GCM AAD that ends up on the record (D-448).
        trace_id = uuid.uuid4().hex
        timestamp = datetime.now(UTC).isoformat()

        request_body, response_body = self._raw_bodies(messages, tools, kwargs, response)

        encryption_envelope: EncryptedEnvelope | None = None
        if (
            self._encryption_config.enabled
            and self._wrapping_key is not None
            and (request_body is not None or response_body is not None)
        ):
            encryption_envelope = seal(
                {"request_body": request_body, "response_body": response_body},
                trace_id=trace_id,
                timestamp=timestamp,
                wrapping_key=self._wrapping_key,
                key_ref=self._encryption_config.key_ref,
            )
            request_body = None
            response_body = None

        return _PreparedBodies(
            trace_id=trace_id,
            timestamp=timestamp,
            request_body=request_body,
            response_body=response_body,
            encryption=encryption_envelope,
        )

    def _build_trace_record(
        self,
        response: LLMResponse,
        cost: float,
        phase_timings: dict[str, float],
        prepared: "_PreparedBodies",
        kwargs: dict[str, Any],
        status: Literal["success", "error", "timeout"] = "success",
        error: str | None = None,
    ) -> TraceRecord:
        """Build a TraceRecord from invoke() data and pre-built bodies."""
        lineage = kwargs.get("lineage", self._lineage_default)
        if lineage is not None:
            lineage = self._cap_body(lineage, _MAX_LINEAGE_BYTES)

        classification = resolve_classification(
            kwargs.get("classification"), self._classification_floor
        )

        # Extract retry metadata injected by RetryModule
        attempt_number: int = kwargs.get("_retry_attempt", 0)
        retry_group_id: str | None = kwargs.get("_retry_group_id")

        usage = response.usage
        return TraceRecord(
            trace_id=prepared.trace_id,
            timestamp=prepared.timestamp,
            provider=self._inner.name,
            model=response.model,
            agent_label=self._resolve_agent_label(),
            agent_did=self._resolve_agent_did(),
            budget_scope=self._budget_scope,
            request_body=prepared.request_body,
            response_body=prepared.response_body,
            classification=classification,
            encryption=prepared.encryption,
            lineage=lineage,
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

    async def _maybe_audit_disable(self) -> None:
        """Emit one ``config_change`` record the first time capture is off.

        FR-21/D-444: disabling full raw capture must itself be an audited
        act, not a silent flag flip. Fires at most once per module
        instance, before the first invoke's own llm_call record, so an
        operator turning capture off can't blind the forensic trail
        without leaving a trace of having done so.
        """
        if not self._audit_disable_pending:
            return
        self._audit_disable_pending = False
        record = TraceRecord(
            provider=self._inner.name,
            model=self._inner.model_name,
            agent_label=self._resolve_agent_label(),
            agent_did=self._resolve_agent_did(),
            event_type="config_change",
            event_data={
                "field": "store_raw_bodies",
                "from": True,
                "to": False,
                "resolver_identity": self._resolve_agent_did() or _UNKNOWN_DID,
            },
        )
        await self._emit_trace(record)

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        await self._maybe_audit_disable()
        with self._span("arcllm.telemetry") as tel_span:
            t0 = time.monotonic()
            try:
                response, cost, total_ms, prepared = await self._invoke_inner(
                    messages, tools, tel_span, t0, **kwargs
                )
            except Exception:
                # FR-4 / C3 — a raising call still records an operational line.
                error_prepared = self._prepare_bodies(messages, tools, kwargs, None)
                self._record_spool(
                    outcome="error",
                    model=None,
                    cost=None,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                    request_body=error_prepared.request_body,
                )
                raise
            self._record_spool(
                outcome="ok",
                model=response.model,
                cost=cost,
                latency_ms=total_ms,
                # Total input context, not just the billed-uncached suffix. With
                # prompt caching, anthropic reports usage.input_tokens as ONLY the
                # new tokens (a fully-cached prompt shows ~2) while the real context
                # sits in cache_read/write — sum them so context-occupancy + token
                # accounting reflect the actual prompt size. Cost stays separate.
                prompt_tokens=(
                    response.usage.input_tokens
                    + (response.usage.cache_read_tokens or 0)
                    + (response.usage.cache_write_tokens or 0)
                ),
                completion_tokens=response.usage.output_tokens,
                # Persist the split too — hit-rate + in/out/cache surfacing needs
                # the breakdown, not just the summed prompt_tokens above.
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_write_tokens=response.usage.cache_write_tokens,
                request_body=prepared.request_body,
                response_body=prepared.response_body,
            )
            return response

    async def _invoke_inner(
        self,
        messages: list[Message],
        tools: list[Tool] | None,
        tel_span: trace.Span,
        t0: float,
        **kwargs: Any,
    ) -> tuple[LLMResponse, float, float, _PreparedBodies]:
        # Budget pre-check (before calling inner provider)
        budget_meta = self._check_budget_pre_call(tel_span, **kwargs)

        # Strip internal metadata keys before forwarding to inner provider.
        # NOTE: "classification" is intentionally NOT stripped here — it is
        # also a RoutingModule kwarg (content-classification routing, further
        # inside the stack) that RoutingModule itself pops before reaching
        # the terminal adapter. Only "lineage" is arcllm-internal with no
        # downstream consumer.
        inner_kwargs = {
            k: v for k, v in kwargs.items() if not k.startswith("_") and k != "lineage"
        }

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

        # Bodies built exactly ONCE here (H1/M4) — shared by the trace
        # record below and the spool call back in invoke().
        prepared = self._prepare_bodies(messages, tools, kwargs, response)

        # Build and emit trace record (fire-and-forget for trace_store)
        if self._trace_store is not None or self._on_event is not None:
            record = self._build_trace_record(response, cost, phase_timings, prepared, kwargs)
            await self._emit_trace(record)

        return response, cost, total_ms, prepared

    def _record_spool(
        self,
        *,
        outcome: str,
        model: str | None,
        cost: float | None,
        latency_ms: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        request_body: dict[str, Any] | None = None,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        """Append one ``llm_call`` operational record to the arcstore spool.

        On by default (``arcstore_enabled``); imports only ``arcstore.spool``.
        ``record()`` is itself fail-open, so this never breaks the call.

        Raw request/response bodies ride ``extra`` only when ``store_raw_bodies``
        is enabled (they arrive non-None) — so the UI can show the actual call,
        not just metadata. Metadata-only is the federal/CUI default.
        """
        if not self._arcstore_enabled:
            return
        extra: dict[str, Any] = {}
        if request_body is not None:
            extra["request_body"] = request_body
        if response_body is not None:
            extra["response_body"] = response_body
        _spool_record(
            _SpoolRecord(
                kind="llm_call",
                actor_did=self._resolve_agent_did() or _UNKNOWN_DID,
                model=model,
                provider=self._inner.name,
                agent_label=self._resolve_agent_label(),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                outcome=outcome,
                extra=extra,
            )
        )
