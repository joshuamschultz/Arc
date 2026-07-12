"""Consolidation — the slow "sleep" path that turns a day into durable memory.

This is the orchestrator (SDD 4.4, REQ-030/031/034). Off the hot path, over a
*bounded window* of the raw stream, it:

1. **extracts facts** (additive, `was:` trails, corroboration-grown confidence);
2. **mints insights** (the analogical abstractions, cues wired as graph nodes);
3. **promotes procedures** (action-sequences seen >= threshold — zero-LLM);
4. **decays** unreinforced edges (salience-slowed, so a rare-but-vital edge lives);
5. **merges near-duplicate cues** to bound controlled-vocabulary drift (T-054);
6. **reindexes** the touched chunks so surface recall sees the new curated files.

Every mutation emits an ``AuditEvent`` to the injected sink (REQ-034), so the whole
cycle is reconstructable from a tamper-evident chain.

**Crash safety** (absorbing ``DeepConsolidator``'s write-ahead manifest): a
``in_progress`` marker is written *before* any file mutation and cleared only on
success. Because the curated markdown is truth and the SQLite index is disposable,
recovery from an interrupted run is simply "rebuild the index from the files that
did land, then clear the marker" — deterministic, no LLM, no partial state.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from arcmemory import distill
from arcmemory.config import MemoryConfig
from arcmemory.curate import curate_for_distillation
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, IndexRebuilder, embed_or_none
from arcmemory.index.surface import SurfaceIndex, _cosine
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import (
    ConsolidationResult,
    DaySummary,
    Event,
    Fact,
    Insight,
    Procedure,
    Scope,
    TimeWindow,
)

_MANIFEST_NAME = ".consolidate-manifest.json"
_LAST_RUN_NAME = ".consolidate-last-run"
# Cosine at/above which two cue embeddings are treated as the same concept (T-054).
_CUE_MERGE_THRESHOLD = 0.92


def _wikilink_bullets(bullets: list[str], name_to_slug: dict[str, str]) -> list[str]:
    """Wrap the first (longest) known entity NAME in each bullet as ``[[slug]]``.

    Conservative — one link per bullet, longest name first — so a day's people bullets
    become hoppable to their entity cards without fragile global text rewrites.
    """
    names = sorted(name_to_slug, key=len, reverse=True)
    linked: list[str] = []
    for bullet in bullets:
        for name in names:
            slug = name_to_slug[name]
            if name and name in bullet and f"[[{slug}]]" not in bullet:
                bullet = bullet.replace(name, f"[[{slug}]]", 1)
                break
        linked.append(bullet)
    return linked


class Consolidator:
    """Orchestrates one bounded consolidation run for a single agent scope."""

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        scope: Scope,
        *,
        distiller: distill.Distiller,
        config: MemoryConfig | None = None,
        audit_sink: AuditSink | None = None,
        embedder: Embedder | None = None,
        seed_vocabulary: Iterable[str] | None = None,
        promote_threshold: int = 2,
    ) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._scope = scope
        self._distiller = distiller
        self._cfg = config or MemoryConfig()
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._embedder = embedder
        self._seed_vocab = set(seed_vocabulary or [])
        self._promote_threshold = promote_threshold

        self._graph = WeightedGraph(db, self._cfg)
        self._semantic = SemanticStore(workspace, self._graph, scope=scope.key)
        self._insights = InsightStore(workspace)
        self._procedures = ProceduralStore(workspace)
        self._daily = DailyNotesStore(workspace)
        self._episodic = EpisodicStore(db, workspace)
        self._surface = SurfaceIndex(
            db,
            workspace,
            scope,
            config=self._cfg,
            embedder=embedder,
            audit_sink=self._audit,
            seed_vocabulary=self._seed_vocab,
        )
        self._manifest_path = self._workspace / "memory" / _MANIFEST_NAME
        self._last_run_path = self._workspace / "memory" / _LAST_RUN_NAME

    @property
    def pending_recovery(self) -> bool:
        """Whether a prior run was interrupted (a stale manifest is present)."""
        return self._manifest_path.exists()

    def last_run(self) -> datetime | None:
        """When consolidation last completed (None if it has never run here)."""
        if not self._last_run_path.exists():
            return None
        try:
            return datetime.fromisoformat(self._last_run_path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def due(self, *, now: datetime, interval_minutes: float) -> bool:
        """Whether the cadence interval has elapsed since the last run.

        The persisted stamp is what keeps the slow LLM sleep-path off the hot path:
        an agent may ask to consolidate every turn, but it only actually runs once
        the interval passes (or if it has never run in this workspace).
        """
        last = self.last_run()
        return last is None or (now - last) >= timedelta(minutes=interval_minutes)

    async def run(
        self, window: TimeWindow | None = None, *, now: datetime | None = None
    ) -> ConsolidationResult:
        """Run one bounded consolidation cycle; return its mutation counts."""
        window = window or TimeWindow()
        now = now or datetime.now(UTC)
        events = [e for e in self._episodic.events(self._scope.key) if window.contains(e.ts)]
        events = curate_for_distillation(events, self._cfg)
        if not events:
            self._stamp_last_run(now)
            return ConsolidationResult()

        self._begin_manifest(len(events))
        facts = await self._extract_facts(events)
        insights = await self._mint_insights(events, [f for _, f in facts])
        procedures = self._promote_procedures(events) + await self._extract_procedures(events)
        days = await self._summarize_days(events)
        decayed = self._decay(now)
        await self._merge_cues_audited()
        await self._surface.index_if_needed()
        self._commit_manifest()
        self._stamp_last_run(now)

        return ConsolidationResult(
            facts_updated=len(facts),
            insights_minted=len(insights),
            procedures_promoted=len(procedures),
            days_summarized=len(days),
            edges_decayed=decayed,
            files_rewritten=len(facts) + len(insights) + len(procedures) + len(days),
            window_events=len(events),
        )

    # -- steps -------------------------------------------------------------

    async def _extract_facts(self, events: list[Event]) -> list[tuple[str, Fact]]:
        """Distill + apply facts; audit each mutation + the file it rewrote."""
        applied = await distill.extract_facts(
            events, distiller=self._distiller, store=self._semantic, config=self._cfg
        )
        for slug, fact in applied:
            self._emit("memory.fact_updated", f"{slug}:{fact.predicate}")
            self._emit("memory.file_rewritten", str(self._semantic.path_for(slug)))
        return applied

    async def _mint_insights(self, events: list[Event], facts: list[Fact]) -> list[Insight]:
        """Distill + mint insights; audit each mutation + the file it rewrote."""
        minted = await distill.mint_insights(
            events,
            facts,
            distiller=self._distiller,
            store=self._insights,
            graph=self._graph,
            scope=self._scope,
            config=self._cfg,
        )
        for insight in minted:
            self._emit("memory.insight_minted", insight.id)
            self._emit("memory.file_rewritten", str(self._insights.path_for(insight.id)))
        return minted

    def _promote_procedures(self, events: list[Event]) -> list[Procedure]:
        """Promote repeated action-sequences; audit each promotion + its file."""
        promoted = self._procedures.promote(events, threshold=self._promote_threshold)
        for procedure in promoted:
            self._emit("memory.procedure_promoted", procedure.slug)
            self._emit("memory.file_rewritten", str(self._procedures.path_for(procedure.slug)))
        return promoted

    async def _extract_procedures(self, events: list[Event]) -> list[Procedure]:
        """Distill reusable how-to procedures (LLM); audit each upsert + its file."""
        extracted = await distill.extract_procedures(
            events, distiller=self._distiller, store=self._procedures, config=self._cfg
        )
        for procedure in extracted:
            self._emit("memory.procedure_extracted", procedure.slug)
            self._emit("memory.file_rewritten", str(self._procedures.path_for(procedure.slug)))
        return extracted

    async def _summarize_days(self, events: list[Event]) -> list[DaySummary]:
        """Condense each day into meeting-minutes notes; link people to entities; audit.

        One bounded completion per day (over that day's slice of the window), merged
        additively into the existing file so a later run grows the notes rather than
        clobbering them. People bullets are wiki-linked to their entity cards so an
        agent can hop. The raw transcript stays in the episodic stream — never here.
        """
        name_to_slug = self._entity_name_map()
        by_day: dict[str, list[Event]] = defaultdict(list)
        for event in events:
            by_day[event.ts[:10]].append(event)
        written: list[DaySummary] = []
        for day in sorted(by_day):
            day_events = by_day[day]
            draft = await self._summarize_day_chunked(day_events)
            additions = DaySummary(
                day=day,
                timeline=draft.timeline,
                discussions=draft.discussions,
                decisions=draft.decisions,
                people=_wikilink_bullets(draft.people, name_to_slug),
                goals=draft.goals,
                tasks=draft.tasks,
            )
            summary = self._daily.merge(additions, day_events)
            if summary is None:
                continue
            self._emit("memory.day_summarized", day)
            self._emit("memory.file_rewritten", str(self._daily.path_for(day)))
            written.append(summary)
        return written

    async def _summarize_day_chunked(self, day_events: list[Event]) -> distill.DaySummaryDraft:
        """Summarize a day, splitting an over-budget day into sequential calls.

        Each chunk yields a partial meeting-minutes draft; the bullet lists are
        concatenated so a busy day never overflows the distiller context.
        """
        chunks = distill.chunk_events(day_events, self._cfg.distill_max_input_tokens)
        merged = distill.DaySummaryDraft()
        for chunk in chunks:
            part = await self._distiller.summarize_day(chunk)
            merged.timeline += part.timeline
            merged.discussions += part.discussions
            merged.decisions += part.decisions
            merged.people += part.people
            merged.goals += part.goals
            merged.tasks += part.tasks
        return merged

    def _entity_name_map(self) -> dict[str, str]:
        """Map each known entity NAME -> its slug (for wiki-linking day notes)."""
        pairs: dict[str, str] = {}
        for slug in self._semantic.slugs():
            entity = self._semantic.read(slug)
            if entity is not None and entity.name:
                pairs[entity.name] = slug
        return pairs

    def _decay(self, now: datetime) -> int:
        """Decay unreinforced edges; audit the sweep (one event, the count)."""
        decayed = self._graph.decay(self._scope.key, now=now)
        self._emit("memory.edges_decayed", "graph", extra={"forgotten": decayed})
        return decayed

    async def merge_cues(self) -> list[tuple[str, str]]:
        """Merge near-duplicate cues (embedding-cluster); repoint their links.

        Returns the ``(merged_from, merged_into)`` pairs. Cues are embedded through
        the injected seam; when no embedder is available this is a no-op (drift is
        bounded elsewhere by the controlled vocabulary).
        """
        cues = self._all_cues()
        if len(cues) < 2:
            return []
        embedded = await embed_or_none(self._embedder, cues)
        if embedded is None:
            return []
        vectors = dict(zip(cues, embedded, strict=True))
        canonical_of = self._cluster_cues(cues, vectors)

        merges: list[tuple[str, str]] = []
        for cue, canonical in canonical_of.items():
            if cue == canonical:
                continue
            self._repoint_cue(cue, canonical)
            merges.append((cue, canonical))
        return merges

    # -- cue-merge helpers -------------------------------------------------

    def _all_cues(self) -> list[str]:
        """Every distinct cue across all insight cards (sorted, deterministic)."""
        seen: set[str] = set()
        for insight_id in self._insights.all_ids():
            card = self._insights.read(insight_id)
            if card is not None:
                seen.update(card.cues)
        return sorted(seen)

    def _cluster_cues(self, cues: list[str], vectors: dict[str, list[float]]) -> dict[str, str]:
        """Greedily assign each cue to a canonical (first-seen) cluster representative."""
        canonicals: list[str] = []
        canonical_of: dict[str, str] = {}
        for cue in cues:
            match = next(
                (
                    c
                    for c in canonicals
                    if _cosine(vectors[cue], vectors[c]) >= _CUE_MERGE_THRESHOLD
                ),
                None,
            )
            if match is None:
                canonicals.append(cue)
                canonical_of[cue] = cue
            else:
                canonical_of[cue] = match
        return canonical_of

    def _repoint_cue(self, cue: str, canonical: str) -> None:
        """Rewrite every insight referencing ``cue`` to ``canonical`` + move its edges."""
        for insight_id in self._insights.all_ids():
            card = self._insights.read(insight_id)
            if card is None or cue not in card.cues:
                continue
            card.cues = [canonical if c == cue else c for c in card.cues]
            # dedup while preserving order
            card.cues = list(dict.fromkeys(card.cues))
            self._insights.write(card)
        self._graph.rename_node(self._scope.key, cue, canonical)

    async def _merge_cues_audited(self) -> None:
        """Run cue merge and audit each (part of the nightly hygiene, T-054)."""
        for merged_from, merged_into in await self.merge_cues():
            self._emit("memory.cue_merged", f"{merged_from}->{merged_into}")

    # -- crash-safe manifest ----------------------------------------------

    def _begin_manifest(self, window_events: int) -> None:
        """Write the write-ahead crash marker before any file mutation."""
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(
            json.dumps(
                {
                    "status": "in_progress",
                    "started": datetime.now(UTC).isoformat(),
                    "scope": self._scope.key,
                    "window_events": window_events,
                }
            ),
            encoding="utf-8",
        )

    def _commit_manifest(self) -> None:
        """Clear the marker on a clean run."""
        self._manifest_path.unlink(missing_ok=True)

    def _stamp_last_run(self, now: datetime) -> None:
        """Persist the consolidation time so the cadence gate survives a restart."""
        self._last_run_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_run_path.write_text(now.isoformat(), encoding="utf-8")

    async def recover(self) -> bool:
        """Recover from an interrupted run: rebuild the index from truth, clear marker.

        Truth is the curated markdown + raw stream; the SQLite index is disposable,
        so a deterministic rebuild restores a consistent state regardless of where
        the crash landed. Returns True if a recovery was performed.
        """
        if not self.pending_recovery:
            return False
        await IndexRebuilder(
            self._db,
            self._workspace,
            self._scope,
            config=self._cfg,
            embedder=self._embedder,
            seed_vocabulary=self._seed_vocab,
        ).rebuild()
        self._manifest_path.unlink(missing_ok=True)
        self._emit("memory.consolidation_recovered", self._scope.key)
        return True

    # -- audit -------------------------------------------------------------

    def _emit(
        self,
        action: str,
        target: str,
        classification: str = "unclassified",
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit one tamper-evident consolidation audit event (REQ-034, AU-2)."""
        emit(
            AuditEvent(
                actor_did=self._scope.agent_did,
                action=action,
                target=target,
                outcome="allow",
                classification=classification,
                tier=self._cfg.tier,
                extra=extra or {},
            ),
            self._audit,
        )


__all__ = ["Consolidator"]
