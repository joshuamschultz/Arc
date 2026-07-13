"""Phase 10 — the arcllm-backed embedder + distiller adapters.

Proves the loop-safe async bridge translates arcllm's responses into arcmemory's
seam contracts, and that an unavailable embedder degrades (never crashes). arcllm is
stubbed (monkeypatched ``arcllm.embed`` / a fake provider) so these stay offline and
deterministic — the point is the *adapter* logic, not a live model.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import arcllm
import pytest

from arcmemory.arcllm_seam import ArcLLMDistiller, ArcLLMEmbedder
from arcmemory.distill import EntityRef
from arcmemory.index.rebuild import EmbeddingUnavailableError
from arcmemory.types import Event, Fact

# -- ArcLLMEmbedder ---------------------------------------------------------


async def test_embedder_returns_arcllm_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_embed(texts: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["texts"] = texts
        return SimpleNamespace(vectors=[[1.0, 2.0, 3.0]])

    monkeypatch.setattr(arcllm, "embed", fake_embed)
    embedder = ArcLLMEmbedder(model="m", backend="local")

    assert await embedder.embed_texts(["hello"]) == [[1.0, 2.0, 3.0]]
    assert seen["texts"] == ["hello"] and seen["model"] == "m" and seen["backend"] == "local"


async def test_embedder_empty_input_skips_arcllm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("arcllm.embed must not be called for empty input")

    monkeypatch.setattr(arcllm, "embed", boom)
    assert await ArcLLMEmbedder(model="m").embed_texts([]) == []


async def test_embedder_unavailable_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable(*_a: Any, **_k: Any) -> Any:
        raise arcllm.ArcLLMEmbeddingUnavailableError("m", "backend not installed")

    monkeypatch.setattr(arcllm, "embed", unavailable)
    with pytest.raises(EmbeddingUnavailableError):
        await ArcLLMEmbedder(model="m").embed_texts(["x"])


# -- ArcLLMDistiller --------------------------------------------------------


class _FakeProvider:
    """A minimal arcllm provider: records calls, returns a canned response."""

    def __init__(
        self, *, parsed: dict[str, Any] | None = None, content: str | None = None
    ) -> None:
        self._parsed = parsed
        self._content = content
        self.invocations: list[dict[str, Any]] = []

    async def invoke(self, messages: list[Any], *, response_format: Any = None) -> Any:
        self.invocations.append({"response_format": response_format})
        return SimpleNamespace(parsed_content=self._parsed, content=self._content)


def _factory(provider: _FakeProvider) -> Any:
    """A factory shaped like ``arcllm.load_model``: returns the provider DIRECTLY.

    The provider is invoked via ``await provider.invoke(...)`` — it is NOT an async
    context manager. (Wrapping it in ``@asynccontextmanager`` here is what let the
    ``async with`` distiller bug pass tests while throwing on every real run.)
    """

    def make() -> Any:
        return provider

    return make


async def test_distiller_extracts_facts_from_parsed_content() -> None:
    provider = _FakeProvider(
        parsed={"facts": [{"slug": "alice", "predicate": "role", "value": "manager"}]}
    )
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.extract_facts([Event(event_id="e0", scope="s", kind="obs", text="t")])

    assert result.facts[0].slug == "alice" and result.facts[0].value == "manager"


async def test_distiller_mints_insights_from_parsed_content() -> None:
    provider = _FakeProvider(
        parsed={
            "insights": [
                {"id": "p1", "statement": "s", "trigger": "t", "cues": ["c"], "instances": ["e0"]}
            ]
        }
    )
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.mint_insights(
        [Event(event_id="e0", scope="s", kind="obs", text="t")],
        [Fact(predicate="role", value="manager", confidence=0.8)],
    )

    assert result.insights[0].id == "p1" and result.insights[0].cues == ["c"]


async def test_distiller_parses_raw_content_when_no_parsed_object() -> None:
    provider = _FakeProvider(
        content='{"facts": [{"slug": "bob", "predicate": "p", "value": "v"}]}'
    )
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.extract_facts([Event(event_id="e0", scope="s", kind="obs", text="t")])

    assert result.facts[0].slug == "bob"


async def test_distiller_summarizes_day_from_parsed_content() -> None:
    provider = _FakeProvider(
        parsed={"timeline": ["09:00 shipped the fix"], "people": ["Alice"], "decisions": []}
    )
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.summarize_day([Event(event_id="e0", scope="s", kind="obs", text="t")])

    assert result.timeline == ["09:00 shipped the fix"] and result.people == ["Alice"]


async def test_distiller_extracts_procedures_from_parsed_content() -> None:
    provider = _FakeProvider(
        parsed={
            "procedures": [
                {"slug": "deploy", "title": "Deploy", "when_to_use": "shipping", "steps": ["a", "b"]}
            ]
        }
    )
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.extract_procedures(
        [Event(event_id="e0", scope="s", kind="obs", text="t")]
    )

    assert result.procedures[0].slug == "deploy"
    assert result.procedures[0].when_to_use == "shipping"
    assert result.procedures[0].steps == ["a", "b"]


async def test_distiller_invokes_provider_directly_not_as_context_manager() -> None:
    """Regression: load_model returns a provider, not an async CM. A provider whose
    only protocol is ``await invoke(...)`` — no ``__aenter__`` — must work."""
    provider = _FakeProvider(parsed={"facts": []})
    assert not hasattr(provider, "__aenter__")  # exactly what load_model returns
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    await distiller.extract_facts([Event(event_id="e0", scope="s", kind="obs", text="t")])
    assert len(provider.invocations) == 1  # the provider was invoked directly


async def test_distiller_falls_back_to_plain_when_json_mode_unsupported() -> None:
    class _Anthropicish(_FakeProvider):
        async def invoke(self, messages: list[Any], *, response_format: Any = None) -> Any:
            self.invocations.append({"response_format": response_format})
            if response_format is not None:  # anthropic path: no server-side JSON mode
                raise arcllm.ArcLLMConfigError("json mode unsupported")
            return SimpleNamespace(parsed_content=None, content='{"facts": []}')

    provider = _Anthropicish()
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.extract_facts([Event(event_id="e0", scope="s", kind="obs", text="t")])

    assert result.facts == []
    # Tried JSON-mode first, then retried plain — two invocations.
    assert len(provider.invocations) == 2
    assert provider.invocations[0]["response_format"] is not None
    assert provider.invocations[1]["response_format"] is None


async def test_distiller_tolerates_garbage_content() -> None:
    provider = _FakeProvider(content="not json at all")
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    result = await distiller.extract_facts([Event(event_id="e0", scope="s", kind="obs", text="t")])

    assert result.facts == []  # empty object, never a crash


def _ref(slug: str) -> EntityRef:
    return EntityRef(slug=slug, name=slug.title(), entity_type="place", facts=[])


async def test_confirm_entity_merges_keeps_only_valid_subgroups() -> None:
    """Confirmed subgroups are filtered to the cluster's own slugs and to >= 2 members."""
    provider = _FakeProvider(
        parsed={"merge": [["austin-texas", "austin-tx"], ["austin-metro"], ["austin-tx", "zzz"]]}
    )
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    confirmed = await distiller.confirm_entity_merges(
        [[_ref("austin-texas"), _ref("austin-tx"), _ref("austin-metro")]]
    )

    # ["austin-metro"] dropped (< 2); ["austin-tx","zzz"] -> ["austin-tx"] dropped (< 2).
    assert confirmed == [["austin-texas", "austin-tx"]]
    assert len(provider.invocations) == 1  # one bounded call for the one cluster


async def test_confirm_entity_merges_empty_when_model_declines() -> None:
    provider = _FakeProvider(parsed={"merge": []})
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    confirmed = await distiller.confirm_entity_merges([[_ref("a"), _ref("b")]])

    assert confirmed == []  # nothing merged when the model says none are the same


async def test_confirm_entity_merges_skips_singleton_cluster_without_a_call() -> None:
    provider = _FakeProvider(parsed={"merge": [["a", "b"]]})
    distiller = ArcLLMDistiller(_factory(provider), model="m")

    confirmed = await distiller.confirm_entity_merges([[_ref("solo")]])

    assert confirmed == []
    assert provider.invocations == []  # a < 2 cluster costs no LLM call
