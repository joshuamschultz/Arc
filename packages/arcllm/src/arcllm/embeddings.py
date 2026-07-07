"""Embeddings capability — arcllm owns embedding *inference* and nothing else.

SPEC-041 Phase 1. arcmemory (and any consumer) obtains vectors only through
``embed``; arcllm persists, indexes, and ranks nothing (REQ-041). The default
backend is the offline, deterministic ``all-MiniLM-L6-v2`` model (via the
optional ``arcllm[local]`` extra); an optional OpenAI-wire provider endpoint
and a ``none`` sentinel round out the three backends (T-012).

Every embed call rides the *existing* SPEC-038 budget plumbing — the shared
``BudgetAccumulator`` registry, ``calculate_cost``, and the standard
``ArcLLMBudgetError`` breach — so embed spend aggregates against the same
per-scope budget as completions, and emits an ``llm_call`` telemetry record
(T-011, LLM10).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel

from arcllm.exceptions import ArcLLMConfigError, ArcLLMEmbeddingUnavailableError
from arcllm.modules.base import resolve_enforcement
from arcllm.modules.telemetry_budget import (
    BudgetAccumulator,
    get_or_create_accumulator,
    validate_budget_scope,
)
from arcllm.modules.telemetry_cost import calculate_cost
from arcllm.types import Usage

if TYPE_CHECKING:  # keep httpx + arcstore off the module-import hot path
    import httpx
    from arcstore.records import SpoolRecord

logger = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"
_MINILM_DIMS = 384
_UNKNOWN_DID = "did:arc:unknown"


class EmbeddingResponse(BaseModel):
    """Normalized embedding result — vectors plus their shape and provenance."""

    vectors: list[list[float]]
    dims: int
    model: str
    usage: Usage


class EmbeddingProvider(ABC):
    """One backend that turns texts into vectors. arcmemory depends on this,
    never on a concrete provider (mirrors ``LLMProvider`` for completions)."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Resolved embedding-model identifier this backend targets."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        """Embed ``texts`` into vectors. Raises ArcLLMEmbeddingUnavailableError
        when this backend cannot serve (the 'none' signal)."""


def _count_tokens(texts: list[str]) -> int:
    """Cheap, deterministic input-token estimate for budget accounting.

    Whitespace-word count (at least 1 per non-empty text). No tokenizer
    download — the estimate only feeds cost arithmetic, not the model.
    """
    return sum(max(1, len(t.split())) for t in texts)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _load_sentence_transformer(model: str) -> Any:
    """Import + construct a ``sentence-transformers`` model.

    Isolated in one function so a caller (or test) can distinguish "extra not
    installed" from a genuine model-load error, and so tests can monkeypatch
    the ``[local]`` extra as absent.
    """
    from sentence_transformers import SentenceTransformer  # arcllm[local] extra

    return SentenceTransformer(model)


class LocalEmbedder(EmbeddingProvider):
    """Offline ``sentence-transformers`` backend (default all-MiniLM-L6-v2).

    Deterministic — a fixed model yields a fixed vector for a given text, so
    arcmemory's tests are reproducible. The model loads lazily on first
    ``embed`` (importing arcllm never pulls torch). When the ``[local]`` extra
    is absent, the load surfaces as ArcLLMEmbeddingUnavailableError — the
    clean 'none' signal, never a bare ImportError crash.
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._st: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model

    def _ensure_model(self) -> Any:
        if self._st is None:
            try:
                self._st = _load_sentence_transformer(self._model)
            except ImportError as e:
                raise ArcLLMEmbeddingUnavailableError(
                    self._model,
                    "the arcllm[local] extra (sentence-transformers) is not installed",
                ) from e
        return self._st

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        model = self._ensure_model()
        # Offload the CPU-bound encode so the event loop is never blocked.
        # normalize_embeddings -> unit vectors, so downstream cosine == dot.
        raw = await asyncio.to_thread(
            model.encode, texts, convert_to_numpy=True, normalize_embeddings=True
        )
        vectors = [[float(x) for x in row] for row in raw]
        dims = len(vectors[0]) if vectors else _MINILM_DIMS
        tokens = _count_tokens(texts)
        return EmbeddingResponse(
            vectors=vectors,
            dims=dims,
            model=self._model,
            usage=Usage(input_tokens=tokens, output_tokens=0, total_tokens=tokens),
        )


class NoneEmbedder(EmbeddingProvider):
    """Sentinel backend — always signals 'no embedder available' (REQ-041).

    Lets a deployment declare, explicitly, that embeddings are off so
    arcmemory degrades to BM25 + graph rather than silently guessing.
    """

    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        raise ArcLLMEmbeddingUnavailableError(self._model, "backend 'none' selected")


class ProviderEmbedder(EmbeddingProvider):
    """Optional remote embeddings endpoint (OpenAI-compatible ``/embeddings``).

    The opt-in path for enterprises with a hosted embedder; federal air-gapped
    deployments use ``LocalEmbedder`` instead (ADR-019). Persists/ranks nothing.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        import httpx

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        try:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": texts},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        finally:
            if self._client is None:
                await client.aclose()

        vectors = [[float(x) for x in item["embedding"]] for item in data["data"]]
        dims = len(vectors[0]) if vectors else 0
        usage_raw = data.get("usage") or {}
        tokens = int(usage_raw.get("prompt_tokens", _count_tokens(texts)))
        return EmbeddingResponse(
            vectors=vectors,
            dims=dims,
            model=self._model,
            usage=Usage(input_tokens=tokens, output_tokens=0, total_tokens=tokens),
        )


# ---------------------------------------------------------------------------
# Backend resolution (cached — a local model reload per call is a cold-start tax)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_local_cache: dict[str, LocalEmbedder] = {}


def clear_embedder_cache() -> None:
    """Drop cached local backends (test isolation / config reload)."""
    with _cache_lock:
        _local_cache.clear()


def resolve_embedder(
    model: str,
    *,
    backend: str = "local",
    base_url: str | None = None,
    api_key: str = "",
) -> EmbeddingProvider:
    """Return the ``EmbeddingProvider`` for ``backend``.

    ``local`` backends are cached per model so repeated calls don't reload the
    weights. ``provider`` requires ``base_url``; ``none`` is the sentinel.
    """
    if backend == "none":
        return NoneEmbedder(model)
    if backend == "provider":
        if not base_url:
            raise ArcLLMConfigError("embedding backend 'provider' requires a 'base_url'")
        return ProviderEmbedder(model, base_url=base_url, api_key=api_key)
    if backend == "local":
        cached = _local_cache.get(model)
        if cached is None:
            with _cache_lock:
                cached = _local_cache.get(model)
                if cached is None:
                    cached = LocalEmbedder(model)
                    _local_cache[model] = cached
        return cached
    raise ArcLLMConfigError(
        f"Unknown embedding backend '{backend}'. Use 'local', 'provider', or 'none'."
    )


# ---------------------------------------------------------------------------
# Budget — reuse of the SPEC-038 accumulator + standard breach (T-011)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BudgetPlan:
    scope: str
    accumulator: BudgetAccumulator
    monthly_limit: float | None
    daily_limit: float | None
    per_call_max: float | None
    enforcement: str


def _parse_budget(telemetry: dict[str, Any]) -> _BudgetPlan | None:
    """Build a budget plan from the telemetry config, or ``None`` when no scope
    is set. Spend is tracked whenever a ``budget_scope`` is present (embed spend
    aggregates onto the agent's shared budget); limits enforce only when set."""
    scope = telemetry.get("budget_scope")
    if not scope:
        return None
    validate_budget_scope(scope)
    return _BudgetPlan(
        scope=scope,
        accumulator=get_or_create_accumulator(scope),
        monthly_limit=telemetry.get("monthly_limit_usd"),
        daily_limit=telemetry.get("daily_limit_usd"),
        per_call_max=telemetry.get("per_call_max_usd"),
        enforcement=resolve_enforcement(telemetry, default="block"),
    )


def _breach(
    plan: _BudgetPlan,
    limit_type: str,
    limit_usd: float,
    current: float,
    estimated: float | None,
) -> None:
    """Enforce one limit: raise the standard breach (block) or warn."""
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
        "embed budget %s limit exceeded for '%s' (warn mode): limit=$%.6f",
        limit_type,
        plan.scope,
        limit_usd,
    )


def _budget_pre_check(plan: _BudgetPlan, pre_tokens: int, cost_input_per_1m: float) -> None:
    """Pre-flight per-call estimate + cumulative check, before the embed call."""
    if plan.per_call_max is not None:
        estimated = pre_tokens * cost_input_per_1m / 1_000_000
        if estimated > plan.per_call_max:
            _breach(plan, "per_call", plan.per_call_max, plan.accumulator.monthly_spend, estimated)

    monthly = plan.monthly_limit if plan.monthly_limit is not None else float("inf")
    daily = plan.daily_limit if plan.daily_limit is not None else float("inf")
    exceeded = plan.accumulator.check_limits(monthly, daily)
    if exceeded == "monthly" and plan.monthly_limit is not None:
        _breach(plan, "monthly", plan.monthly_limit, plan.accumulator.monthly_spend, None)
    elif exceeded == "daily" and plan.daily_limit is not None:
        _breach(plan, "daily", plan.daily_limit, plan.accumulator.daily_spend, None)


# ---------------------------------------------------------------------------
# Telemetry — one llm_call record per embed (reuses the arcstore spool)
# ---------------------------------------------------------------------------


def _emit_telemetry(
    telemetry: dict[str, Any],
    on_event: Callable[[SpoolRecord], None] | None,
    *,
    provider_label: str,
    model: str,
    usage: Usage,
    cost: float,
    latency_ms: float,
) -> None:
    """Emit an ``llm_call`` telemetry record for one embed (T-011, AU-2)."""
    from arcstore.records import SpoolRecord
    from arcstore.spool import record as spool_record

    event = SpoolRecord(
        kind="llm_call",
        actor_did=telemetry.get("agent_did") or _UNKNOWN_DID,
        model=model,
        provider=provider_label,
        agent_label=telemetry.get("agent_label"),
        prompt_tokens=usage.input_tokens,
        completion_tokens=0,
        cost_usd=cost,
        latency_ms=latency_ms,
        outcome="ok",
    )
    if on_event is not None:
        on_event(event)
    if telemetry.get("arcstore_enabled", True):
        spool_record(event)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def embed(
    texts: list[str],
    *,
    model: str,
    provider: EmbeddingProvider | None = None,
    backend: str = "local",
    telemetry: dict[str, Any] | None = None,
    on_event: Callable[[SpoolRecord], None] | None = None,
) -> EmbeddingResponse:
    """Embed ``texts`` into vectors, budget-routed and telemetered.

    Args:
        texts: Inputs to embed.
        model: Embedding-model identifier (e.g. ``all-MiniLM-L6-v2``).
        provider: An explicit backend to use (dependency injection). When
            ``None``, one is resolved from ``backend`` + ``model``.
        backend: ``"local"`` (default), ``"provider"``, or ``"none"``.
        telemetry: SPEC-038 budget + telemetry config, reusing the completion
            keys — ``budget_scope``, ``monthly_limit_usd``, ``daily_limit_usd``,
            ``per_call_max_usd``, ``cost_input_per_1m``, ``enforcement``,
            ``agent_did``, ``agent_label``, ``arcstore_enabled``.
        on_event: Optional callback fired with the ``SpoolRecord`` telemetry.

    Returns:
        The ``EmbeddingResponse``.

    Raises:
        ArcLLMEmbeddingUnavailableError: No embedder available (the 'none'
            signal) — callers degrade to BM25 + graph.
        ArcLLMBudgetError: The call would exceed a configured budget limit.
    """
    tel = telemetry or {}
    embedder = provider if provider is not None else resolve_embedder(model, backend=backend)
    provider_label = "custom" if provider is not None else backend
    cost_input_per_1m = tel.get("cost_input_per_1m", 0.0)

    plan = _parse_budget(tel)
    if plan is not None:
        _budget_pre_check(plan, _count_tokens(texts), cost_input_per_1m)

    tracer = trace.get_tracer("arcllm")
    with tracer.start_as_current_span("arcllm.embed") as span:
        span.set_attribute("arcllm.embed.model", model)
        span.set_attribute("arcllm.embed.count", len(texts))
        t0 = time.monotonic()
        response = await embedder.embed(texts)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)

        cost = calculate_cost(response.usage, input_per_1m=cost_input_per_1m, output_per_1m=0.0)
        if plan is not None:
            plan.accumulator.deduct(max(0.0, cost))
        span.set_attribute("arcllm.embed.cost_usd", cost)
        span.set_attribute("arcllm.embed.dims", response.dims)

    _emit_telemetry(
        tel,
        on_event,
        provider_label=provider_label,
        model=response.model,
        usage=response.usage,
        cost=cost,
        latency_ms=latency_ms,
    )
    return response
