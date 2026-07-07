"""Structural / analogical retrieval — the centerpiece (SDD 7, REQ-050..054).

Surface recall answers "what past text *looks* like this query". Structural recall
answers the harder, differentiating question: "what past *pattern* does this present
situation *instance* — even with zero surface overlap?". It works in **abstraction
space**, never surface space, over the two things a minted ``Insight`` carries that a
raw episode lacks: a mechanism-level ``trigger`` and a set of abstract ``cues``.

Two mutually-reinforcing channels, over two separate stores:

* **(a) trigger-embedding** (``trigger_match``). The current situation, abstracted
  (default: the reused turn summary — OQ-1, *no new LLM call*), is embedded and cosine
  matched against the ``insight_trigger`` vectors — a table kept **apart** from the
  surface ``vec0`` chunks so surface noise cannot drown a minted trigger. Once both
  sides are stated at the mechanism level, the abstraction collapses the surface
  distance that defeats raw embeddings.
* **(b) cue-graph spreading activation** (``cue_match``). The situation lights a few
  abstract cue nodes; activation flows over the weighted graph (the *same*
  ``WeightedGraph.spreading_activation`` the surface channel uses — ACT-R fan effect,
  hop-capped, zero-LLM) to the insight nodes whose cues are active. The graph edges
  *are* the learned "situation-shape -> pattern" mapping, so this fires with **zero
  surface overlap** to the instances.

``match`` fuses the two: a candidate must clear **both** channels (conjunctive gating,
SDD R-8 false-positive control) before it is promoted — which is exactly why a
never-recurring ``guessed`` insight, whose cue edges decay below the forget floor over
time, silently **decays out**. Promoted candidates are confidence-gated (``known`` ->
actionable anchor; ``guessed`` -> surfaced tentatively, *verify first*), then enriched
("spot, then enrich": instances + N-hop neighbors + surrounding raw stream), with an
optional, tier-gated cross-encoder rerank over the *small* candidate set.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Protocol

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from pydantic import BaseModel, Field

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.surface import _cosine
from arcmemory.security import content_hash
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tagging import tag_entities
from arcmemory.types import Confidence, Entity, Event, Insight, Recall, Scope, Situation

_RRF_K = 60


class Reranker(Protocol):
    """Cross-encoder seam (D-9): score how well the situation instances each candidate.

    Injected, never imported — production wires an arcllm cross-encoder, tests inject
    a stub. Its verdict only *reorders* the small candidate set; it never enters
    agent-visible content (LLM09/LLM10: bounded, off the hot path).
    """

    async def rerank(self, situation: str, candidates: list[str]) -> list[float]: ...


class InsightBundle(BaseModel):
    """The enriched neighborhood of one matched insight (SDD 7 "spot, then enrich")."""

    insight: Insight
    instances: list[Event] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    adjacent_insights: list[Insight] = Field(default_factory=list)
    stream_context: list[Event] = Field(default_factory=list)


class StructuralResult(BaseModel):
    """The bounded structural-channel result: confidence-gated recalls + degrade flag."""

    recalls: list[Recall] = Field(default_factory=list)
    degraded: bool = False


class StructuralIndex:
    """Trigger-embedding + cue-graph spreading over minted insights for one scope."""

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        scope: Scope,
        *,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._scope = scope
        self._cfg = config or MemoryConfig()
        self._embedder = embedder
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._graph = WeightedGraph(db, self._cfg)
        self._insights = InsightStore(workspace)
        self._episodic = EpisodicStore(db, workspace)
        self._semantic = SemanticStore(workspace, self._graph, scope=scope.key)
        self._entities_dir = self._workspace / "memory" / "entities"

    # -- T-060: trigger index (kept apart from surface vec0) ----------------

    async def trigger_index(self) -> int:
        """Embed each insight ``trigger`` into the separate table; content-gated.

        Only new or changed triggers are re-embedded (LLM10 budget), so re-indexing a
        stable insight set is free. Returns how many triggers were (re)embedded. A
        no-op when no embedder is injected — the trigger channel then simply degrades.
        """
        if self._embedder is None:
            return 0
        conn = self._db.connect()
        stored = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT insight_id, content_hash FROM insight_trigger WHERE scope=?",
                (self._scope.key,),
            ).fetchall()
        }
        pending: list[tuple[str, str]] = []  # (insight_id, trigger_text)
        for insight_id in self._insights.all_ids():
            card = self._insights.read(insight_id)
            if card is None:
                continue
            if stored.get(insight_id) != content_hash(card.trigger):
                pending.append((insight_id, card.trigger))
        if not pending:
            return 0

        vectors = await embed_or_none(self._embedder, [trigger for _, trigger in pending])
        if vectors is None:
            return 0
        for (insight_id, trigger), vector in zip(pending, vectors, strict=True):
            conn.execute(
                "INSERT OR REPLACE INTO insight_trigger "
                "(insight_id, scope, content_hash, embedding) VALUES (?, ?, ?, ?)",
                (insight_id, self._scope.key, content_hash(trigger), _pack(vector)),
            )
        conn.commit()
        return len(pending)

    # -- T-061: channel (a) trigger-embedding -------------------------------

    async def trigger_match(
        self, situation: Situation, *, top_k: int = 5
    ) -> list[tuple[str, float]] | None:
        """Cosine-match the abstracted situation against insight triggers.

        Returns ``(insight_id, cosine)`` above ``struct_trigger_min``, best first, or
        ``None`` when no embedder is available (the caller then falls back to the
        cue-graph channel only — SDD degrade).
        """
        vectors = await embed_or_none(self._embedder, [_abstract(situation)])
        if not vectors:
            return None
        query = vectors[0]
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT insight_id, embedding FROM insight_trigger WHERE scope=?",
            (self._scope.key,),
        ).fetchall()
        scored = [(insight_id, _cosine(query, _unpack(blob))) for insight_id, blob in rows]
        ranked = sorted(
            ((iid, score) for iid, score in scored if score > self._cfg.struct_trigger_min),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[:top_k]

    # -- T-062: channel (b) cue-graph spreading activation ------------------

    def cue_match(self, situation: Situation, *, top_k: int = 5) -> list[tuple[str, float]]:
        """Spread activation from the situation's cue nodes to the insight nodes.

        The situation's cues (from the turn summary, OQ-1; or tagged from its text
        against the cue vocabulary) light the graph; activation flows over the learned
        weighted edges to the insights whose cues are active. Returns
        ``(insight_id, activation)`` above ``struct_activation_min``, best first.
        """
        cues = self._situation_cues(situation)
        if not cues:
            return []
        activation = self._graph.spreading_activation(self._scope.key, dict.fromkeys(cues, 1.0))
        ranked = sorted(
            (
                (insight_id, activation[insight_id])
                for insight_id in self._insights.all_ids()
                if activation.get(insight_id, 0.0) > self._cfg.struct_activation_min
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[:top_k]

    # -- match: fuse + conjunctive-gate + confidence-gate + rerank ----------

    async def match(
        self,
        situation: Situation,
        *,
        top_k: int = 5,
        reranker: Reranker | None = None,
    ) -> StructuralResult:
        """Retrieve the insights the situation structurally instances (bounded).

        Both channels must agree before a candidate is promoted (conjunctive gating);
        when the trigger channel is unavailable (no embedder) the cue-graph channel
        stands alone (degrade). Promoted candidates are RRF-fused, confidence-gated,
        and optionally reranked over the small set. Decay is a consolidation sweep
        (``WeightedGraph.decay``), so a match reads the post-sweep edge weights — a
        guessed insight whose cue edges have decayed below the floor no longer clears
        the cue channel, and conjunctive gating drops it.
        """
        trig = await self.trigger_match(situation, top_k=max(top_k * 4, top_k))
        cue = self.cue_match(situation, top_k=max(top_k * 4, top_k))
        degraded = trig is None

        cue_ids = {iid for iid, _ in cue}
        if degraded:
            promoted = cue_ids  # cue-graph only
            channels = [[iid for iid, _ in cue]]
        else:
            trig_ids = {iid for iid, _ in trig or []}
            promoted = trig_ids & cue_ids  # conjunctive gating (R-8)
            channels = [[iid for iid, _ in trig or []], [iid for iid, _ in cue]]

        fused = self._rrf(channels, promoted)
        recalls = [r for iid, score in fused if (r := self._to_recall(iid, score)) is not None]
        recalls = await self._maybe_rerank(situation, recalls, reranker)
        if degraded:
            self._emit_degraded(situation)
        return StructuralResult(recalls=recalls[:top_k], degraded=degraded)

    # -- T-064: enrichment — spot, then enrich ------------------------------

    def enrich(self, insight_id: str, *, hops: int | None = None) -> InsightBundle:
        """Bundle a matched insight with its instances, neighbors, and stream context.

        Anchored on the abstraction: from the insight we traverse to the episodes it
        generalizes (``instances``), the entities those episodes mention, the adjacent
        insights sharing its cues (bounded hops), and the raw-stream events immediately
        surrounding each instance (``enrich_stream_radius``).
        """
        card = self._insights.read(insight_id)
        if card is None:
            raise KeyError(insight_id)
        stream = self._episodic.events(self._scope.key)
        instance_ids = set(card.instances)
        instances = [e for e in stream if e.event_id in instance_ids]
        return InsightBundle(
            insight=card,
            instances=instances,
            entities=self._related_entities(instances, card),
            adjacent_insights=self._adjacent_insights(card, hops),
            stream_context=self._stream_context(stream, instance_ids),
        )

    # -- helpers ------------------------------------------------------------

    def _situation_cues(self, situation: Situation) -> list[str]:
        """The active graph nodes a situation lights (the cue-channel seeds).

        Explicit ``cues`` (the turn's tagged entities / active concept nodes, passed
        through the production seam) win. Otherwise we tag the abstraction against the
        **entity + cue** vocabulary — i.e. the real graph nodes — NOT the cue phrases
        alone. Seeding from concrete entity/concept nodes is what lets a genuinely
        different-domain situation reach an insight *through* the graph (its nodes
        spread over learned edges to the insight's cues), instead of only firing when
        the raw query literally contains a cue phrase.
        """
        if situation.cues:
            return situation.cues
        vocab = self._cue_vocabulary() | self._entity_vocabulary()
        return tag_entities(_abstract(situation), vocab)

    def _cue_vocabulary(self) -> set[str]:
        """Every cue across all insight cards (the controlled abstract vocabulary)."""
        vocab: set[str] = set()
        for insight_id in self._insights.all_ids():
            card = self._insights.read(insight_id)
            if card is not None:
                vocab.update(card.cues)
        return vocab

    def _rrf(self, channels: list[list[str]], promoted: set[str]) -> list[tuple[str, float]]:
        """Reciprocal-rank-fuse the channels, restricted to the promoted candidates."""
        scores: dict[str, float] = {}
        for ranked in channels:
            for rank, insight_id in enumerate(ranked):
                if insight_id in promoted:
                    scores[insight_id] = scores.get(insight_id, 0.0) + 1.0 / (_RRF_K + rank)
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    def _to_recall(self, insight_id: str, score: float) -> Recall | None:
        """Hydrate a promoted insight into a confidence-gated ``Recall`` (T-063)."""
        card = self._insights.read(insight_id)
        if card is None:
            return None
        verify_first = card.status is Confidence.GUESSED
        return Recall(
            source=insight_id,
            content=card.statement,
            score=score,
            kind="structural",
            confidence=card.status,
            # The card's stored label is the dominating classification of the episodes
            # it generalizes (set at mint) — carry it, never a literal, so the no-read-up
            # gate drops a SECRET-derived insight and fails closed on an unknown one.
            classification=card.classification,
            verify_first=verify_first,
        )

    async def _maybe_rerank(
        self, situation: Situation, recalls: list[Recall], reranker: Reranker | None
    ) -> list[Recall]:
        """Tier-gated cross-encoder rerank over the small candidate set (T-065)."""
        if reranker is None or not self._should_rerank(recalls):
            return recalls
        scores = await reranker.rerank(_abstract(situation), [r.content for r in recalls])
        order = sorted(range(len(recalls)), key=lambda i: -scores[i])
        return [recalls[i] for i in order]

    def _should_rerank(self, recalls: list[Recall]) -> bool:
        """Enterprise/federal always rerank; personal only on a close top-1/top-2 margin."""
        if not recalls:
            return False
        if self._cfg.tier in ("enterprise", "federal"):
            return True
        if len(recalls) < 2:
            return False
        return (recalls[0].score - recalls[1].score) < self._cfg.rerank_margin

    def _related_entities(self, instances: list[Event], card: Insight) -> list[Entity]:
        """Entity cards mentioned in the instance episodes (bounded, deterministic)."""
        vocab = self._entity_vocabulary()
        if not vocab:
            return []
        slugs: set[str] = set()
        for event in instances:
            slugs.update(tag_entities(event.text, vocab))
        slugs.update(tag_entities(card.statement, vocab))
        return [e for slug in sorted(slugs) if (e := self._semantic.read(slug)) is not None]

    def _adjacent_insights(self, card: Insight, hops: int | None) -> list[Insight]:
        """Other insights reachable from this insight's cue nodes (bounded hops)."""
        if not card.cues:
            return []
        activation = self._graph.spreading_activation(
            self._scope.key, dict.fromkeys(card.cues, 1.0), max_hops=hops
        )
        out: list[Insight] = []
        for insight_id in self._insights.all_ids():
            if insight_id == card.id or activation.get(insight_id, 0.0) <= 0.0:
                continue
            neighbor = self._insights.read(insight_id)
            if neighbor is not None:
                out.append(neighbor)
        return out

    def _stream_context(self, stream: list[Event], instance_ids: set[str]) -> list[Event]:
        """Raw-stream events within ``enrich_stream_radius`` of any instance."""
        radius = self._cfg.enrich_stream_radius
        keep: set[int] = set()
        for i, event in enumerate(stream):
            if event.event_id in instance_ids:
                keep.update(range(max(0, i - radius), min(len(stream), i + radius + 1)))
        return [stream[i] for i in sorted(keep) if stream[i].event_id not in instance_ids]

    def _entity_vocabulary(self) -> set[str]:
        """Slugs of existing entity files (the deterministic tagging vocabulary)."""
        if not self._entities_dir.exists():
            return set()
        return {p.stem for p in self._entities_dir.glob("*.md")}

    def _emit_degraded(self, situation: Situation) -> None:
        """Signal (never raise) that structural retrieval ran without the trigger channel."""
        emit(
            AuditEvent(
                actor_did=self._scope.agent_did,
                action="recall.degraded",
                target="structural.match",
                outcome="allow",
                tier=self._cfg.tier,
                payload_hash=content_hash(_abstract(situation)),
                extra={"reason": "embeddings_unavailable", "channel": "structural"},
            ),
            self._audit,
        )


def _abstract(situation: Situation) -> str:
    """The situation's abstraction: its reused turn summary, else its raw text (OQ-1)."""
    return situation.summary or situation.text


def _pack(vector: list[float]) -> bytes:
    """Serialize a float vector to a compact float32 blob."""
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    """Inverse of ``_pack`` — recover the float vector from its blob."""
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


__all__ = ["InsightBundle", "Reranker", "StructuralIndex", "StructuralResult"]
