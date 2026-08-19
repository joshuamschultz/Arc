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
from typing import Any

import arcllm
from arcprompt import load_stock

from arcmemory.distill import (
    DaySummaryDraft,
    EntityRef,
    EventExtraction,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.rebuild import EmbeddingUnavailableError
from arcmemory.types import Event, Fact, Procedure

# A factory that yields a *fresh* arcllm provider for one call. An arcllm model
# (``load_model``) is invoked directly — ``await provider.invoke(...)`` — exactly as
# arcrun/arcagent use it; it is NOT an async context manager (that was the SPEC-041
# distiller bug: ``async with`` threw ``'QueueModule' object does not support the
# asynchronous context manager protocol`` on every real consolidation).
ProviderFactory = Callable[[], Any]


class ArcLLMEmbedder:
    """arcmemory ``Embedder`` seam backed by ``arcllm.embed`` (async, loop-safe).

    ``backend`` selects the arcllm embedding backend: ``local`` (the default —
    on-device sentence-transformers, the federal air-gap path), ``provider`` (a
    remote OpenAI-compatible ``/embeddings`` endpoint, which needs ``base_url``),
    or ``none`` (deliberately off). The remote endpoint is resolved *here* rather
    than through ``arcllm.embed``'s backend name, because that entry point takes no
    connection details — passing the resolved provider is how a base_url reaches it.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        backend: str = "local",
        provider: Any = None,
        base_url: str | None = None,
        api_key: str = "",
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        self._model = model or arcllm.DEFAULT_EMBED_MODEL
        self._backend = backend
        self._provider = provider
        self._base_url = base_url
        self._api_key = api_key
        self._telemetry = telemetry

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed via arcllm; translate any 'cannot serve' into the degrade signal.

        Both an unavailable backend (``ArcLLMEmbeddingUnavailableError`` — the
        ``[local]`` extra absent) and a misconfigured one (``ArcLLMConfigError`` —
        an unknown backend name, a ``provider`` backend with no ``base_url``)
        become :class:`EmbeddingUnavailableError`, which the ``embed_or_none``
        funnel collapses to a dropped vector channel. A typo in an agent's TOML
        must degrade recall, never crash it.
        """
        if not texts:
            return []
        try:
            response = await arcllm.embed(
                texts,
                model=self._model,
                backend=self._backend,
                provider=self._resolve_provider(),
                telemetry=self._telemetry,
            )
        except (
            arcllm.ArcLLMEmbeddingUnavailableError,
            arcllm.ArcLLMConfigError,
        ) as exc:  # -> BM25 + graph degrade
            raise EmbeddingUnavailableError(str(exc)) from exc
        return [[float(x) for x in vector] for vector in response.vectors]

    def _resolve_provider(self) -> Any:
        """The explicit backend instance, when connection details were supplied."""
        if self._provider is not None or self._base_url is None:
            return self._provider
        return arcllm.resolve_embedder(
            self._model,
            backend=self._backend,
            base_url=self._base_url,
            api_key=self._api_key,
        )


def unwrap_envelope(data: dict[str, Any], expected: str) -> dict[str, Any]:
    """Return the object holding ``expected``, opening a single-key envelope if needed.

    Structured-output modes sometimes return the answer nested inside one provider
    key — ``{"$parameter": {...}}``, ``{"output": {...}}`` — instead of at the top
    level. Every distiller call reads its own key straight off the parsed dict, so a
    wrapped response silently became "no facts", "no insights", "merge nothing": a
    total failure indistinguishable from a considered negative answer. It was
    observed live on both the merge-confirm and step-consolidation calls.

    Narrow on purpose: exactly one key, whose value is a dict, and only when
    ``expected`` is not already present — so a legitimate one-key answer is never
    mistaken for an envelope.

    Also handles the inverse the same providers produce: the answer wrapped in a
    *repeat* of its own key — ``{"insights": {"insights": [...]}}`` — where the
    top-level ``expected`` is present but its value is the whole object again, not
    the list the model field wants. Left unpeeled, every consolidation crashed
    with ``list_type`` (``InsightMint.insights`` got a dict), which aborted the
    entire sleep pass — no daily notes, no entities, fleet-wide.
    """
    if len(data) == 1 and expected not in data:
        inner = next(iter(data.values()))
        if isinstance(inner, dict):
            data = inner
    value = data.get(expected)
    if isinstance(value, dict) and expected in value:
        return {**data, expected: value[expected]}
    return data


class ArcLLMDistiller:
    """arcmemory ``Distiller`` seam backed by an arcllm structured completion.

    Bounded, single-shot calls (no agentic loop — OQ-3): fact extraction, insight
    minting, procedure + life-event extraction, day summary. A fresh provider is
    loaded per call via ``provider_factory`` and invoked directly
    (``await provider.invoke(...)``) — the arcllm model is not an async context manager.
    """

    def __init__(self, provider_factory: ProviderFactory, *, model: str | None = None) -> None:
        self._provider_factory = provider_factory
        self._model = model

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        """One structured completion → additive semantic facts (REQ-031/032/033)."""
        data = await self._complete(
            load_stock("arcmemory", "distill_fact"), self._render_events(events), "facts"
        )
        return FactExtraction.model_validate(data)

    async def mint_insights(self, events: list[Event], facts: list[Fact]) -> InsightMint:
        """One structured completion → minted abstractions, the centerpiece (REQ-050)."""
        user = f"{self._render_events(events)}\n\nKnown facts:\n{self._render_facts(facts)}"
        data = await self._complete(load_stock("arcmemory", "distill_insight"), user, "insights")
        return InsightMint.model_validate(data)

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        """One structured completion → the MERGED how-to cards (methods evolve).

        The stored cards go into the prompt so the model edits a real playbook rather
        than re-deriving one from this window alone — the difference between a method
        that accumulates and one that is overwritten by whatever was mentioned today.
        """
        user = (
            f"{self._render_events(events)}\n\n"
            f"Existing procedure cards:\n{self._render_procedures(existing)}"
        )
        data = await self._complete(
            load_stock("arcmemory", "distill_procedure"), user, "procedures"
        )
        return ProcedureExtraction.model_validate(data)

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        """One structured completion → things that happened in the USER's life."""
        data = await self._complete(
            load_stock("arcmemory", "distill_event"), self._render_events(episodes), "events"
        )
        return EventExtraction.model_validate(data)

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        """One structured completion → meeting-minutes daily notes (chronological)."""
        data = await self._complete(
            load_stock("arcmemory", "distill_day"), self._render_events(events), "timeline"
        )
        return DaySummaryDraft.model_validate(data)

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        """One bounded call → the existing slug this candidate IS, or None (new)."""
        listing = "\n".join(f"- {slug}" for slug in candidates)
        user = f"New candidate: {name} (type: {entity_type})\nExisting cards:\n{listing}"
        data = await self._complete(load_stock("arcmemory", "distill_disambiguate"), user, "slug")
        chosen = data.get("slug")
        if not isinstance(chosen, str) or not chosen.strip():
            return None
        return chosen if chosen in candidates else None

    async def consolidate_steps(self, steps: list[str]) -> list[tuple[str, list[int]]]:
        """Rewrite a merged procedure's repeated steps into one ordered sequence.

        Merging cards is a union, so eleven differently-worded versions of one method
        produce a 54-step card — faithfully preserved and far too long to follow.
        This collapses the repetition.

        Each returned step carries the 1-based input numbers it covers, which is what
        makes the rewrite checkable: the caller verifies every input step is accounted
        for and keeps the union if any is missing. Without that, "consolidate" and
        "silently drop the operator's instructions" are the same call.
        """
        if len(steps) < 2:
            return []
        numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(steps, start=1))
        data = await self._complete(
            load_stock("arcmemory", "consolidate_steps"), numbered, "steps"
        )
        out: list[tuple[str, list[int]]] = []
        for item in data.get("steps", []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            covers = [n for n in item.get("covers", []) if isinstance(n, int)]
            if text and covers:
                out.append((text, covers))
        return out

    async def find_contradictions(self, group: list[EntityRef]) -> list[str]:
        """Which of these same-name same-type cards a fact positively rules out.

        The narrow half of de-dup. Whether the cards are the same entity is already
        settled by identical name and type; asked that open question instead, the model
        declined real duplicates run after run and answered inconsistently on an
        unchanged prompt. "Does any fact contradict?" is checkable, and an empty answer
        is the ordinary one.

        Only slugs from the input are honoured, so a hallucinated name cannot block a
        merge, and only >= 2 cards are worth asking about.
        """
        if len(group) < 2:
            return []
        slugs = {ref.slug for ref in group}
        data = await self._complete(
            load_stock("arcmemory", "distill_find_contradictions"),
            self._render_cards(group),
            "contradicting",
        )
        named = data.get("contradicting", [])
        if not isinstance(named, list):
            return []
        return [s for s in dict.fromkeys(named) if isinstance(s, str) and s in slugs]

    async def confirm_entity_merges(self, groups: list[list[EntityRef]]) -> list[list[str]]:
        """One bounded, conservative call per candidate cluster -> confirmed same-entity subgroups.

        A card without a similar same-type neighbour never reaches here (the caller only
        clusters >= 2 cards), so the LLM is spent only on high-probability duplicates. Each
        confirmed subgroup is filtered back to the cluster's own slugs and to >= 2 members,
        so a hallucinated or singleton answer can never trigger a merge.
        """
        confirmed: list[list[str]] = []
        for group in groups:
            if len(group) < 2:
                continue
            slugs = {ref.slug for ref in group}
            data = await self._complete(
                load_stock("arcmemory", "distill_merge_confirm"),
                self._render_cards(group),
                "merge",
            )
            for sub in data.get("merge", []):
                if not isinstance(sub, list):
                    continue
                picked = [s for s in dict.fromkeys(sub) if isinstance(s, str) and s in slugs]
                if len(picked) >= 2:
                    confirmed.append(picked)
        return confirmed

    async def _complete(self, system: str, user: str, expects: str = "") -> dict[str, Any]:
        """Run one bounded JSON completion and parse the object (provider-agnostic).

        ``expects`` names the key the caller will read, so a provider that nests its
        answer in a single-key envelope is unwrapped here rather than read as an
        empty answer at every call site (see :func:`unwrap_envelope`).
        """
        messages = [
            arcllm.Message(role="system", content=system),
            arcllm.Message(role="user", content=user),
        ]
        provider = self._provider_factory()
        response = await provider.invoke(messages, response_format={"type": "json_object"})
        parsed = response.parsed_content
        data = parsed if isinstance(parsed, dict) else self._parse(response.content)
        return unwrap_envelope(data, expects) if expects else data

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
        """Compact, id + time-anchored rendering (instance-citation + chronology)."""
        return "\n".join(f"- [{e.event_id}] {e.ts[11:16]} ({e.kind}) {e.text}" for e in events)

    @staticmethod
    def _render_facts(facts: list[Fact]) -> str:
        """One line per known fact, for insight grounding."""
        return "\n".join(f"- {f.predicate}: {f.value}" for f in facts) or "(none)"

    @staticmethod
    def _render_procedures(procedures: list[Procedure]) -> str:
        """Each stored card in full (trigger + numbered steps) — the merge base."""
        if not procedures:
            return "(none)"
        lines: list[str] = []
        for card in procedures:
            lines.append(
                f"- slug={card.slug} | title={card.title} | when_to_use={card.when_to_use}"
            )
            lines += [f"    {i}. {step}" for i, step in enumerate(card.steps, start=1)]
        return "\n".join(lines)

    @staticmethod
    def _render_cards(cards: list[EntityRef]) -> str:
        """One line per candidate card (slug + name + type + key facts) for the confirmer."""
        lines: list[str] = []
        for card in cards:
            facts = "; ".join(card.facts) if card.facts else "(no facts)"
            lines.append(
                f"- slug={card.slug} | name={card.name} | type={card.entity_type} | {facts}"
            )
        return "\n".join(lines)


__all__ = ["ArcLLMDistiller", "ArcLLMEmbedder", "ProviderFactory"]
