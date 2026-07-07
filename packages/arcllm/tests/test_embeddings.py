"""Embeddings capability — public ``embed`` API, budget routing, backends.

SPEC-041 Phase 1 (T-010/T-011/T-012). Verifies:
- ``embed()`` returns a typed ``EmbeddingResponse`` (vectors + dims + model + usage).
- An embed call debits the shared SPEC-038 budget accumulator and emits
  telemetry; an over-budget call raises the standard ``ArcLLMBudgetError``.
- Backend selection: an injected/fake backend, the ``none`` sentinel
  (degrade-cleanly), and the optional local ``all-MiniLM-L6-v2`` backend
  (deterministic, offline) which is skipped when its extra is absent.
"""

from __future__ import annotations

import importlib.util

import pytest

from arcllm.embeddings import (
    EmbeddingProvider,
    EmbeddingResponse,
    clear_embedder_cache,
    embed,
    resolve_embedder,
)
from arcllm.exceptions import ArcLLMBudgetError, ArcLLMEmbeddingUnavailableError
from arcllm.modules.telemetry_budget import clear_budgets, get_or_create_accumulator
from arcllm.types import Usage

_LOCAL_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None


class _FakeEmbedder(EmbeddingProvider):
    """Deterministic in-memory embedder for tests — fixed vectors, no network."""

    def __init__(self, dims: int = 4, *, model: str = "fake-embed") -> None:
        self._dims = dims
        self._model = model
        self.calls: int = 0

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        self.calls += 1
        vectors = [[float(i) for i in range(self._dims)] for _ in texts]
        tokens = sum(max(1, len(t.split())) for t in texts)
        return EmbeddingResponse(
            vectors=vectors,
            dims=self._dims,
            model=self._model,
            usage=Usage(input_tokens=tokens, output_tokens=0, total_tokens=tokens),
        )


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear_budgets()
    clear_embedder_cache()
    yield
    clear_budgets()
    clear_embedder_cache()


# ---------------------------------------------------------------------------
# T-010 — public embed API + EmbeddingResponse
# ---------------------------------------------------------------------------


async def test_embed_returns_response_with_matching_dims() -> None:
    fake = _FakeEmbedder(dims=8)
    resp = await embed(["hello world", "second text"], model="fake-embed", provider=fake)

    assert isinstance(resp, EmbeddingResponse)
    assert resp.model == "fake-embed"
    assert resp.dims == 8
    assert len(resp.vectors) == 2
    assert all(len(v) == resp.dims for v in resp.vectors)
    assert resp.usage.input_tokens > 0


# ---------------------------------------------------------------------------
# T-011 — budget routed through the existing SPEC-038 plumbing
# ---------------------------------------------------------------------------


async def test_embed_debits_shared_budget_accumulator() -> None:
    fake = _FakeEmbedder()
    scope = "agent:embed-budget-test"
    await embed(
        ["one two three"],
        model="fake-embed",
        provider=fake,
        telemetry={"budget_scope": scope, "cost_input_per_1m": 10.0},
    )

    acc = get_or_create_accumulator(scope)
    # 3 tokens * $10 / 1M = 3e-5
    assert acc.monthly_spend == pytest.approx(3 * 10.0 / 1_000_000)


async def test_embed_over_budget_raises_standard_breach() -> None:
    fake = _FakeEmbedder()
    scope = "agent:embed-over-budget"
    tel = {
        "budget_scope": scope,
        "cost_input_per_1m": 1_000_000.0,  # $1 per token — one call blows the cap
        "monthly_limit_usd": 2.0,
        "enforcement": "block",
    }
    # First call: 3 tokens -> $3 spent, over the $2 monthly cap.
    await embed(["one two three"], model="fake-embed", provider=fake, telemetry=tel)
    # Second call must see the cumulative breach and raise the standard error.
    with pytest.raises(ArcLLMBudgetError) as exc:
        await embed(["again"], model="fake-embed", provider=fake, telemetry=tel)
    assert exc.value.scope == scope
    assert exc.value.limit_type == "monthly"


async def test_embed_emits_telemetry_event() -> None:
    from arcstore.records import SpoolRecord

    fake = _FakeEmbedder()
    captured: list[SpoolRecord] = []
    await embed(
        ["telemetry please"],
        model="fake-embed",
        provider=fake,
        on_event=captured.append,
    )
    assert len(captured) == 1
    record = captured[0]
    assert record.kind == "llm_call"
    assert record.outcome == "ok"
    assert record.prompt_tokens == 2


# ---------------------------------------------------------------------------
# T-012 — backends: none sentinel (unconditional) + local (skip if absent)
# ---------------------------------------------------------------------------


async def test_none_backend_signals_unavailable() -> None:
    with pytest.raises(ArcLLMEmbeddingUnavailableError):
        await embed(["anything"], model="none", backend="none")


async def test_local_backend_degrades_cleanly_when_extra_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With sentence-transformers unavailable, the local backend must raise the
    'none' signal — never a hard ImportError crash."""
    import arcllm.embeddings as emb

    monkeypatch.setattr(emb, "_load_sentence_transformer", _raise_import)
    with pytest.raises(ArcLLMEmbeddingUnavailableError):
        await embed(["anything"], model="all-MiniLM-L6-v2", backend="local")


def _raise_import(model: str) -> object:
    raise ImportError("sentence-transformers not installed")


@pytest.mark.skipif(not _LOCAL_AVAILABLE, reason="requires arcllm[local] extra")
async def test_local_embedder_is_deterministic_and_offline() -> None:
    embedder = resolve_embedder("all-MiniLM-L6-v2", backend="local")
    a = await embedder.embed(["the quick brown fox"])
    b = await embedder.embed(["the quick brown fox"])
    assert a.dims == 384
    assert a.vectors == b.vectors  # fixed model -> fixed vector


async def test_provider_backend_requires_base_url() -> None:
    from arcllm.exceptions import ArcLLMConfigError

    with pytest.raises(ArcLLMConfigError):
        resolve_embedder("text-embed", backend="provider")


async def test_provider_embedder_parses_openai_wire() -> None:
    import httpx

    from arcllm.embeddings import ProviderEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = ProviderEmbedder("text-embed", base_url="https://x/v1", client=client)
    resp = await embedder.embed(["hello"])
    await client.aclose()

    assert resp.vectors == [[pytest.approx(0.1), pytest.approx(0.2), pytest.approx(0.3)]]
    assert resp.dims == 3
    assert resp.usage.input_tokens == 5


async def test_unknown_backend_raises() -> None:
    from arcllm.exceptions import ArcLLMConfigError

    with pytest.raises(ArcLLMConfigError):
        resolve_embedder("m", backend="bogus")
