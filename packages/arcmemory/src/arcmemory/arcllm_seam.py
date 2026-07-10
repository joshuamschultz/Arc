"""arcllm-backed embedder + distiller adapters (SPEC-041 Phase 10).

These bridge arcmemory's async ``Embedder`` and ``Distiller`` seams onto arcllm.
They live in arcmemory (which already depends on arcllm) so that arcagent's
``select_brain`` can light up semantic recall + LLM distillation without arcagent
holding any memory logic, and without arcmemory's core index/retrieve modules
importing a provider.

**Why this is loop-safe** (the Phase-8 blocker, closed here). The seams are
*async*: ``embed_texts`` / ``extract_facts`` / ``mint_insights`` are awaited from
inside arcmemory's already-async ``retrieve()`` / ``consolidate()`` paths on the
same event loop. There is **no** ``asyncio.run``, no ``run_until_complete``, no
new loop, and no thread that blocks the loop — the earlier "sync seam calling
async arcllm" hazard is gone by construction. arcllm's local embedder itself
offloads the CPU-bound encode via ``asyncio.to_thread`` (see
``arcllm.embeddings.LocalEmbedder``), so even the model call never blocks.

**Degrade stays intact.** When arcllm signals no embedder is available
(``ArcLLMEmbeddingUnavailableError`` — e.g. the ``arcllm[local]`` extra is not
installed), :class:`ArcLLMEmbedder` re-raises it as
:class:`~arcmemory.index.rebuild.EmbeddingUnavailableError`, which every arcmemory
call site funnels to a ``None`` vector channel → BM25 + graph, audited, never a
crash (REQ-041).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import arcllm

from arcmemory.distill import DaySummaryDraft, FactExtraction, InsightMint
from arcmemory.index.rebuild import EmbeddingUnavailableError
from arcmemory.types import Event, Fact

# A factory that yields a *fresh*, self-closing arcllm provider for one call.
# Consolidation is off the hot path and infrequent (threshold/idle-triggered), so
# a per-run connection pool that closes cleanly beats a long-lived one to manage.
ProviderFactory = Callable[[], AbstractAsyncContextManager[Any]]


class ArcLLMEmbedder:
    """arcmemory ``Embedder`` seam backed by ``arcllm.embed`` (async, loop-safe)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        backend: str = "local",
        provider: Any = None,
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        self._model = model or arcllm.DEFAULT_EMBED_MODEL
        self._backend = backend
        self._provider = provider
        self._telemetry = telemetry

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed via arcllm; translate 'no backend' into arcmemory's degrade signal."""
        if not texts:
            return []
        try:
            response = await arcllm.embed(
                texts,
                model=self._model,
                backend=self._backend,
                provider=self._provider,
                telemetry=self._telemetry,
            )
        except arcllm.ArcLLMEmbeddingUnavailableError as exc:  # -> BM25 + graph degrade
            raise EmbeddingUnavailableError(str(exc)) from exc
        return [[float(x) for x in vector] for vector in response.vectors]


_FACT_SYSTEM = (
    "You distill a window of raw agent events into durable semantic facts. "
    "Return ONLY a JSON object of the form "
    '{"facts": [{"slug": str, "predicate": str, "value": str, "hits": int, '
    '"name": str|null, "entity_type": str, "classification": str}]}. '
    "slug is a stable lowercase entity id; predicate/value capture one durable "
    "relation. Emit nothing you cannot ground in the events. No prose."
)

_INSIGHT_SYSTEM = (
    "You mint reusable INSIGHTS — abstractions that recur across situations with "
    "little surface overlap. Return ONLY a JSON object of the form "
    '{"insights": [{"id": str, "statement": str, "trigger": str, '
    '"cues": [str], "instances": [str], "hits": int}]}. '
    "'trigger' states the situation at the MECHANISM level (surface stripped). "
    "'cues' are abstract feature tags from a small controlled vocabulary. "
    "'instances' are the event ids the insight generalizes. No prose."
)

_DAY_SYSTEM = (
    "You condense one day of raw agent events into CURATED daily notes — a "
    "high-signal summary a human can skim, NOT a transcript. Return ONLY a JSON "
    'object of the form {"summary": [str], "people": [str], "decisions": [str], '
    '"tasks": [str]}. '
    "'summary' is a few bullets of what happened / was discussed; 'people' names "
    "the people, places, and organizations that came up; 'decisions' are choices "
    "made; 'tasks' are action items or todos. Each bullet is one short line. Emit "
    "nothing you cannot ground in the events; leave a list empty if it has none. No prose."
)


class ArcLLMDistiller:
    """arcmemory ``Distiller`` seam backed by an arcllm structured completion.

    Two bounded, single-shot calls (no agentic loop — OQ-3). A fresh provider is
    opened per call via ``provider_factory`` and closed on exit, so there is no
    long-lived pool to own and consolidation errors cannot leak a connection.
    """

    def __init__(self, provider_factory: ProviderFactory, *, model: str | None = None) -> None:
        self._provider_factory = provider_factory
        self._model = model

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        """One structured completion → additive semantic facts (REQ-031/032/033)."""
        data = await self._complete(_FACT_SYSTEM, self._render_events(events))
        return FactExtraction.model_validate(data)

    async def mint_insights(self, events: list[Event], facts: list[Fact]) -> InsightMint:
        """One structured completion → minted abstractions, the centerpiece (REQ-050)."""
        user = f"{self._render_events(events)}\n\nKnown facts:\n{self._render_facts(facts)}"
        data = await self._complete(_INSIGHT_SYSTEM, user)
        return InsightMint.model_validate(data)

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        """One structured completion → curated daily-notes bullets (the searchable rollup)."""
        data = await self._complete(_DAY_SYSTEM, self._render_events(events))
        return DaySummaryDraft.model_validate(data)

    async def _complete(self, system: str, user: str) -> dict[str, Any]:
        """Run one bounded JSON completion and parse the object (provider-agnostic)."""
        messages = [
            arcllm.Message(role="system", content=system),
            arcllm.Message(role="user", content=user),
        ]
        async with self._provider_factory() as provider:
            response = await self._invoke(provider, messages)
        parsed = response.parsed_content
        if isinstance(parsed, dict):
            return parsed
        return self._parse(response.content)

    async def _invoke(self, provider: Any, messages: list[Any]) -> Any:
        """Invoke with JSON-mode when supported, plain otherwise (anthropic path)."""
        try:
            return await provider.invoke(messages, response_format={"type": "json_object"})
        except arcllm.ArcLLMConfigError:  # provider without server-side JSON mode
            return await provider.invoke(messages)

    @staticmethod
    def _parse(content: str | None) -> dict[str, Any]:
        """Parse a JSON object from raw completion text (empty object on garbage)."""
        if not content:
            return {}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _render_events(events: list[Event]) -> str:
        """Compact, id-anchored rendering of the window (instance-citation grounding)."""
        return "\n".join(f"- [{e.event_id}] ({e.kind}) {e.text}" for e in events)

    @staticmethod
    def _render_facts(facts: list[Fact]) -> str:
        """One line per known fact, for insight grounding."""
        return "\n".join(f"- {f.predicate}: {f.value}" for f in facts) or "(none)"


__all__ = ["ArcLLMDistiller", "ArcLLMEmbedder", "ProviderFactory"]
