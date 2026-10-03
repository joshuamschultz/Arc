"""Entity identity de-dup — one engine for the nightly pass and ``arc memory dedup``.

The distiller names one real thing differently across runs ("Thesis 5" and
"Thesis 5: Multi-Layer Tuning"), and it files the type differently too ("thing"
and "thesis"). Same-type embedding clusters could never pair those: the types
differ, and a bare label embeds far from its long form. This engine finds them
with three candidate channels, none of which needs the other:

1. **series key** — "Thesis 5", "ai-thesis-5" and "Thesis 5: ..." share
   ``("thesis", "5")``. Different numbers in one series are never candidates.
2. **name tokens** — punctuation-stripped, stop-worded, lightly stemmed name words;
   a pair is a candidate when one token set holds the other or they overlap by half.
3. **embedding** — name cosine above ``entity_merge_candidate_threshold``, across
   types (optional: with no embedder, channels 1-2 still run, and the skip is loud).

Every channel respects the same guards: kinds must be compatible (a person is never
a place; system cards never merge) and classification levels must be EQUAL (a fold
never moves a fact across a level; at federal an unknown label fails closed).

A series pair whose remaining words agree is folded deterministically. Cards with an
identical name and kind go to the narrow contradiction check. Everything else goes to
the LLM confirmer. A fold keeps the richest card as survivor, gives it the most
specific kind and the most descriptive name, records every old name and slug as an
alias, unions facts/links/tags, rewrites inbound links, and writes an audited merge
record. A second pass over the result finds nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from typing import Any

from arctrust.classification import parse_classification

from arcmemory import distill
from arcmemory.config import MemoryConfig
from arcmemory.entity_kind import (
    SYSTEM_KINDS,
    kind_rank,
    kinds_compatible,
    more_specific_kind,
    normalize_kind,
    numbered_label,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.surface import _cosine
from arcmemory.slug import canonical_slug
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Entity

_log = logging.getLogger(__name__)

#: ``emit(action, target, extra)`` — the caller's audit seam.
Emit = Callable[[str, str, dict[str, Any]], None]

_STOPWORDS = frozenset(
    "a an and as at by for from in into is of on or our the to via vs with your my".split()
)
#: Words that carry no identity inside a series name ("AI Thesis 5" = "Thesis 5").
_SERIES_NOISE = frozenset({"ai"})
#: Bound on one LLM confirmation group (LLM10: bounded input per call).
_MAX_GROUP = 8
_TOKEN_OVERLAP = 0.5
_SERIES_RESIDUAL_OVERLAP = 0.34
_REF_FACTS = 4


def _stem(token: str) -> str:
    """Light, deterministic suffix folding so "Agents"/"agent", "Improving"/"improve" meet."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def name_tokens(name: str) -> frozenset[str]:
    """The identity-bearing words of a name: no punctuation, stopwords or plurals."""
    words = canonical_slug(name).split("-")
    return frozenset(
        _stem(w) for w in words if w not in _STOPWORDS and (len(w) > 1 or w.isdigit())
    )


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


@dataclass(frozen=True)
class _Card:
    slug: str
    entity: Entity
    kind: str
    tokens: frozenset[str]
    series: tuple[str, str] | None
    level: int | None  # None = unparseable at this strictness (fails closed)

    @property
    def residual(self) -> frozenset[str]:
        """Name words beyond the series key ("Thesis 5: X" -> {x})."""
        if self.series is None:
            return self.tokens
        label, number = self.series
        return self.tokens - {label, number, _stem(label)} - _SERIES_NOISE


@dataclass(frozen=True)
class MergeGroup:
    """One planned fold: ``folded`` cards into ``survivor``, renamed and re-kinded."""

    survivor: str
    folded: tuple[str, ...]
    name: str
    entity_type: str
    basis: str  # "series" | "confirmed" | "same-name"


@dataclass
class EntityDedupPlan:
    """What a pass would do: certain folds, LLM questions, and guard refusals."""

    entities: int = 0
    certain: list[MergeGroup] = field(default_factory=list)
    exact: list[list[str]] = field(default_factory=list)
    ambiguous: list[list[str]] = field(default_factory=list)
    blocked: list[list[str]] = field(default_factory=list)


@dataclass
class EntityDedupResult:
    """The plan plus the ``(folded, survivor)`` pairs actually merged."""

    plan: EntityDedupPlan
    merged: list[tuple[str, str]] = field(default_factory=list)


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        self._parent[self.find(a)] = self.find(b)

    def groups(self) -> list[list[str]]:
        by_root: dict[str, list[str]] = defaultdict(list)
        for item in self._parent:
            by_root[self.find(item)].append(item)
        return [sorted(members) for members in by_root.values() if len(members) >= 2]


def _is_slug_title(entity: Entity) -> bool:
    """True when the name is only the slug title-cased (no human-chosen wording)."""
    return entity.name == entity.slug.replace("-", " ").title()


def _name_score(card: _Card) -> tuple[bool, int, int]:
    """Richer name first: human-written, more identity words, then longer."""
    return (not _is_slug_title(card.entity), len(card.tokens), len(card.entity.name))


def _survivor_order(card: _Card) -> tuple[int, int, str]:
    """Most facts, then most specific kind, then slug — the card the others fold into."""
    return (-len(card.entity.facts), -kind_rank(card.kind), card.slug)


class EntityDeduper:
    """Plan and apply identity de-dup over one scope's entity cards."""

    def __init__(
        self,
        store: SemanticStore,
        graph: WeightedGraph,
        scope_key: str,
        *,
        config: MemoryConfig,
        embedder: Embedder | None = None,
        confirmer: distill.EntityMergeConfirmer | None = None,
        emit: Emit | None = None,
    ) -> None:
        self._store = store
        self._graph = graph
        self._scope = scope_key
        self._cfg = config
        self._embedder = embedder
        self._confirmer = confirmer
        self._emit_fn = emit
        self._strict = config.tier == "federal"

    # -- planning ------------------------------------------------------------

    async def plan(self) -> EntityDedupPlan:
        """Read every card, run the candidate channels, and classify each cluster."""
        cards = self._cards()
        vectors = await self._vectors(cards)
        # O(N^2) pairwise sweep — off the loop so a large store cannot pin it.
        return await asyncio.to_thread(self._plan_sync, cards, vectors)

    def _cards(self) -> list[_Card]:
        cards: list[_Card] = []
        for slug in self._store.slugs():
            entity = self._store.read(slug)
            if entity is None or normalize_kind(entity.entity_type) in SYSTEM_KINDS:
                continue
            names = [entity.name, slug, *entity.aliases]
            series = next((key for n in names if (key := numbered_label(n)) is not None), None)
            cards.append(
                _Card(
                    slug=slug,
                    entity=entity,
                    kind=normalize_kind(entity.entity_type),
                    tokens=name_tokens(entity.name),
                    series=series,
                    level=self._level(entity.classification),
                )
            )
        return cards

    def _level(self, label: str) -> int | None:
        try:
            return int(parse_classification(label, strict=self._strict))
        except ValueError:
            return None

    async def _vectors(self, cards: list[_Card]) -> dict[str, list[float]] | None:
        if len(cards) < 2:
            return None
        embedded = await embed_or_none(
            self._embedder,
            [c.entity.name for c in cards],
            operation="embed:consolidate-entity-dedup",
        )
        if embedded is None:
            self._skipped("no-embedder")
            return None
        return {card.slug: vec for card, vec in zip(cards, embedded, strict=True)}

    def _plan_sync(
        self, cards: list[_Card], vectors: dict[str, list[float]] | None
    ) -> EntityDedupPlan:
        plan = EntityDedupPlan(entities=len(cards))
        by_slug = {c.slug: c for c in cards}
        certain_uf = _UnionFind(list(by_slug))
        edges: list[tuple[str, str]] = []
        for a, b in combinations(cards, 2):
            if not self._candidate(a, b, vectors):
                continue
            if a.level is None or b.level is None:
                continue  # fail closed: an unparseable label is never merged
            if a.level != b.level:
                plan.blocked.append(sorted([a.slug, b.slug]))
                continue
            edges.append((a.slug, b.slug))
            if self._series_certain(a, b):
                certain_uf.union(a.slug, b.slug)

        representative: dict[str, str] = {slug: slug for slug in by_slug}
        for members in certain_uf.groups():
            group = [by_slug[s] for s in members]
            if not self._consistent_series(group):
                continue
            merge = self._merge_group(group, basis="series")
            plan.certain.append(merge)
            for slug in members:
                representative[slug] = merge.survivor

        rest = _UnionFind(sorted(set(representative.values())))
        for src, dst in edges:
            ra, rb = representative[src], representative[dst]
            if ra != rb:
                rest.union(ra, rb)
        for members in rest.groups():
            if self._same_name(members, by_slug):
                plan.exact.append(members)
            else:
                plan.ambiguous += [
                    chunk
                    for i in range(0, len(members), _MAX_GROUP)
                    if len(chunk := members[i : i + _MAX_GROUP]) >= 2
                ]
        return plan

    def _candidate(self, a: _Card, b: _Card, vectors: dict[str, list[float]] | None) -> bool:
        if not kinds_compatible(a.kind, b.kind):
            return False
        if a.series and b.series and a.series[0] == b.series[0]:
            return a.series[1] == b.series[1]  # same series: only the same number pairs
        if a.entity.name.strip().casefold() == b.entity.name.strip().casefold():
            return True
        small, large = sorted((a.tokens, b.tokens), key=len)
        if len(small) >= 2 and (small <= large or _overlap(a.tokens, b.tokens) >= _TOKEN_OVERLAP):
            return True
        if vectors is None:
            return False
        return (
            _cosine(vectors[a.slug], vectors[b.slug]) >= self._cfg.entity_merge_candidate_threshold
        )

    @staticmethod
    def _series_certain(a: _Card, b: _Card) -> bool:
        """Same series member AND its remaining words agree (one holds the other, or overlap)."""
        if a.series is None or a.series != b.series:
            return False
        ra, rb = a.residual, b.residual
        return (
            not ra
            or not rb
            or ra <= rb
            or rb <= ra
            or _overlap(ra, rb) >= _SERIES_RESIDUAL_OVERLAP
        )

    def _consistent_series(self, group: list[_Card]) -> bool:
        """Every pair in a certain group must itself be certain (no chained guesses)."""
        return all(self._series_certain(a, b) for a, b in combinations(group, 2))

    @staticmethod
    def _same_name(members: list[str], by_slug: dict[str, _Card]) -> bool:
        cards = [by_slug[s] for s in members]
        names = {c.entity.name.strip().casefold() for c in cards}
        return len(names) == 1 and len({c.kind for c in cards}) == 1

    @staticmethod
    def _merge_group(group: list[_Card], *, basis: str) -> MergeGroup:
        ordered = sorted(group, key=_survivor_order)
        survivor = ordered[0]
        series_kinds = [c.series[0] for c in group if c.series is not None]
        kind = more_specific_kind(
            [*series_kinds[:1], survivor.kind, *(c.kind for c in ordered[1:])]
        )
        return MergeGroup(
            survivor=survivor.slug,
            folded=tuple(c.slug for c in ordered[1:]),
            name=max(group, key=_name_score).entity.name,
            entity_type=kind,
            basis=basis,
        )

    # -- applying ------------------------------------------------------------

    async def run(self, *, apply: bool) -> EntityDedupResult:
        """Plan, then (when ``apply``) fold certain groups and ask the LLM about the rest."""
        plan = await self.plan()
        result = EntityDedupResult(plan=plan)
        if apply:
            for group in plan.certain:
                result.merged += self._fold(group)
            result.merged += await self._fold_exact(plan.exact)
            result.merged += await self._fold_confirmed(plan.ambiguous)
        clusters = len(plan.certain) + len(plan.exact) + len(plan.ambiguous)
        self._emit(
            "memory.dedup_pass",
            "memory",
            {
                "entities": plan.entities,
                "clusters": clusters,
                "certain": len(plan.certain),
                "blocked": len(plan.blocked),
                "merged": len(result.merged),
            },
        )
        return result

    async def _fold_exact(self, clusters: list[list[str]]) -> list[tuple[str, str]]:
        """Identical name and kind: fold unless a fact contradicts (asked twice)."""
        if not clusters:
            return []
        if self._confirmer is None:
            self._skipped("no-confirmer")
            return []
        merged: list[tuple[str, str]] = []
        for members in clusters:
            cards = self._live(members)
            refs = [self._ref(c) for c in cards]
            try:
                # Two samples, any flag counts: a fused pair of real people is far
                # worse than a duplicate left for tomorrow night.
                flagged = set(await self._confirmer.find_contradictions(refs))
                flagged |= set(await self._confirmer.find_contradictions(refs))
            except Exception as exc:  # reason: a dead provider must not fuse two people
                _log.warning("arcmemory de-dup: contradiction check failed: %s", exc)
                self._skipped("contradiction-check-failed")
                continue
            keep = [c for c in cards if c.slug not in flagged]
            if len(keep) >= 2:
                merged += self._fold(self._merge_group(keep, basis="same-name"))
        return merged

    async def _fold_confirmed(self, clusters: list[list[str]]) -> list[tuple[str, str]]:
        """Ask the confirmer which sub-groups are one entity; fold only those."""
        if not clusters:
            return []
        if self._confirmer is None:
            self._skipped("no-confirmer")
            return []
        live = [cards for members in clusters if len(cards := self._live(members)) >= 2]
        if not live:
            return []
        try:
            confirmed = await self._confirmer.confirm_entity_merges(
                [[self._ref(c) for c in cards] for cards in live]
            )
        except Exception as exc:  # reason: never fuse two real entities on an error
            _log.warning("arcmemory de-dup: merge confirmation failed: %s", exc)
            self._skipped("confirm-failed")
            return []
        merged: list[tuple[str, str]] = []
        for subgroup in confirmed:
            cards = self._live(subgroup)
            if len(cards) >= 2 and self._guarded(cards):
                merged += self._fold(self._merge_group(cards, basis="confirmed"))
        return merged

    def _guarded(self, cards: list[_Card]) -> bool:
        """Re-check the guards on a model-chosen sub-group (it may pair any two slugs)."""
        levels = {c.level for c in cards}
        if None in levels or len(levels) != 1:
            return False
        return all(kinds_compatible(a.kind, b.kind) for a, b in combinations(cards, 2))

    def _live(self, slugs: list[str]) -> list[_Card]:
        """Re-read the named cards from disk (a prior fold may have consumed some)."""
        wanted = set(slugs)
        return [c for c in self._cards() if c.slug in wanted]

    def _fold(self, group: MergeGroup) -> list[tuple[str, str]]:
        merged: list[tuple[str, str]] = []
        for slug in group.folded:
            folded = self._store.read(slug)
            if folded is None or not self._store.merge_into(
                group.survivor, slug, strict=self._strict
            ):
                continue
            self._graph.rename_node(self._scope, slug, group.survivor)
            self._store.repoint_links(slug, group.survivor)
            record = {
                "survivor": group.survivor,
                "folded": slug,
                "basis": group.basis,
                "entity_type": group.entity_type,
                "folded_name": folded.name,
            }
            self._store.append_merge_record(
                {**record, "ts": datetime.now(UTC).isoformat(), "scope": self._scope}
            )
            self._emit("memory.entity_merged", f"{slug}->{group.survivor}", record)
            merged.append((slug, group.survivor))
        if merged:
            self._store.set_identity(
                group.survivor, name=group.name, entity_type=group.entity_type
            )
        return merged

    @staticmethod
    def _ref(card: _Card) -> distill.EntityRef:
        facts = [f"{f.predicate}: {f.value}" for f in card.entity.facts[:_REF_FACTS]]
        return distill.EntityRef(
            slug=card.slug, name=card.entity.name, entity_type=card.kind, facts=facts
        )

    # -- audit -----------------------------------------------------------------

    def _skipped(self, reason: str) -> None:
        """LOUD degrade: a WARNING plus a ``memory.dedup_skipped`` audit, never silence."""
        _log.warning("arcmemory entity de-dup skipped: %s", reason)
        self._emit("memory.dedup_skipped", "memory", {"reason": reason})

    def _emit(self, action: str, target: str, extra: dict[str, Any]) -> None:
        if self._emit_fn is not None:
            self._emit_fn(action, target, extra)


__all__ = [
    "EntityDedupPlan",
    "EntityDedupResult",
    "EntityDeduper",
    "MergeGroup",
    "name_tokens",
]
