"""Consolidation — the slow "sleep" path that turns a day into durable memory.

This is the orchestrator (SDD 4.4, REQ-030/031/034). Off the hot path, over a
*bounded window* of the raw stream, it:

1. **extracts facts** (additive, `was:` trails, corroboration-grown confidence);
2. **mints insights** (the analogical abstractions, cues wired as graph nodes);
3. **promotes procedures** (action-sequences seen >= threshold — zero-LLM);
4. **decays** unreinforced edges (salience-slowed, so a rare-but-vital edge lives);
5. **merges near-duplicate cues** to bound controlled-vocabulary drift (T-054);
6. **merges duplicate entity cards** — same-type name embeddings generate CANDIDATE
   clusters (a wide cosine bar), one bounded LLM call conservatively confirms which
   cards are the same real-world entity, and only the confirmed sub-groups fold. No
   merge is ever done on embedding similarity alone (a false merge is worse than a
   duplicate), and a card with no similar neighbor costs no LLM call;
7. **reindexes** the touched chunks so surface recall sees the new curated files.

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
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from arctrust.identity import AgentIdentity
from arctrust.policy import PolicyPipeline

from arcmemory import distill
from arcmemory.agent_consolidate import AgenticResult, run_agentic_consolidation
from arcmemory.config import MemoryConfig
from arcmemory.curate import curate_for_distillation
from arcmemory.db import MemoryDB
from arcmemory.hygiene import dedup_workspace, repair_backlinks
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, IndexRebuilder, embed_or_none
from arcmemory.index.surface import SurfaceIndex, _cosine
from arcmemory.react_adapter import ReactLoop, run_react_loop
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tools import build_memory_tools
from arcmemory.types import (
    ConsolidationResult,
    DaySummary,
    Entity,
    Event,
    Fact,
    Insight,
    Procedure,
    Scope,
    TimeWindow,
)

_log = logging.getLogger(__name__)

_MANIFEST_NAME = ".consolidate-manifest.json"
_LAST_RUN_NAME = ".consolidate-last-run"
_HYGIENE_LAST_NAME = ".hygiene-last-run"
# Cosine at/above which two cue embeddings are treated as the same concept (T-054).
_CUE_MERGE_THRESHOLD = 0.92
# Key facts summarized onto an EntityRef for the LLM merge-confirmer (bounded input).
_ENTITY_REF_MAX_FACTS = 5


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
        confirmer: distill.EntityMergeConfirmer | None = None,
        seed_vocabulary: Iterable[str] | None = None,
        model: object | None = None,
        identity: AgentIdentity | None = None,
        policy_pipeline: PolicyPipeline | None = None,
        react_loop: ReactLoop = run_react_loop,
    ) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._scope = scope
        self._distiller = distiller
        self._cfg = config or MemoryConfig()
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._embedder = embedder
        # The LLM gate for slow-path entity de-dup. Absent -> candidates are found but
        # never merged (loud degrade), because merge is never done on embedding alone.
        self._confirmer = confirmer
        self._seed_vocab = set(seed_vocabulary or [])
        # Agentic-engine seams (the DEFAULT DISTILL path). Without a model the
        # engine cannot run, so consolidation falls back to the pipeline distiller.
        self._model = model
        self._identity = identity
        self._policy = policy_pipeline
        self._react_loop = react_loop

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
        self._hygiene_last_path = self._workspace / "memory" / _HYGIENE_LAST_NAME

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
        # Distillation learns from the session CONVERSATION only — the user's turns and the
        # agent's responses. Tool frames and other machinery are dropped (curate.py), so the
        # LLM never distills the agent's own operational mechanics into facts/insights/methods.
        events = curate_for_distillation(events, self._cfg)
        if not events:
            self._stamp_last_run(now)
            return ConsolidationResult()

        self._begin_manifest(len(events))
        facts, insights, procedures, agentic_writes = await self._distill(events)
        days = await self._summarize_days(events)
        decayed = self._decay(now)
        await self._merge_cues_audited()
        await self._merge_entities_audited()
        await self._surface.index_if_needed()
        self._commit_manifest()
        self._stamp_last_run(now)

        return ConsolidationResult(
            facts_updated=len(facts),
            insights_minted=len(insights),
            procedures_promoted=len(procedures),
            days_summarized=len(days),
            edges_decayed=decayed,
            files_rewritten=(
                len(facts) + len(insights) + len(procedures) + len(days) + agentic_writes
            ),
            window_events=len(events),
        )

    # -- distill step: agentic engine (default) with pipeline fallback -----

    async def _distill(
        self, events: list[Event]
    ) -> tuple[list[tuple[str, Fact]], list[Insight], list[Procedure], int]:
        """Route the DISTILL step: agentic engine by default, pipeline as fallback.

        Agentic mode runs the bounded ReAct loop over the memory tools (which write
        cards — facts, insights, and procedures — directly). On a degrade
        (breach/timeout/arcrun-absent, or no model wired) the whole window is finished
        by the pipeline distiller so no data is lost.
        """
        if self._cfg.consolidate_engine == "agentic" and self._model is not None:
            result = await self._run_agentic(events)
            if not result.degraded:
                return [], [], [], result.tool_calls_made
            self._emit("memory.consolidation_degraded", result.reason or "degraded")
        return await self._distill_pipeline(events)

    async def _run_agentic(self, events: list[Event]) -> AgenticResult:
        """Run one bounded agentic consolidation over this scope's memory tools."""
        tools = build_memory_tools(
            workspace=self._workspace,
            db=self._db,
            config=self._cfg,
            caller_did=self._scope.agent_did,
            session_id=self._scope.session_id,
            identity=self._identity,
            policy_pipeline=self._policy,
            audit_sink=self._audit,
            embedder=self._embedder,
            distiller=self._distiller,
        )
        actor_did = self._identity.did if self._identity is not None else self._scope.agent_did
        return await run_agentic_consolidation(
            episodes=events,
            model=self._model,
            tools=tools,
            config=self._cfg,
            actor_did=actor_did,
            react_loop=self._react_loop,
        )

    async def _distill_pipeline(
        self, events: list[Event]
    ) -> tuple[list[tuple[str, Fact]], list[Insight], list[Procedure], int]:
        """The deterministic single-shot distiller path (fallback + engine=pipeline)."""
        facts = await self._extract_facts(events)
        insights = await self._mint_insights(events, [f for _, f in facts])
        procedures = await self._extract_procedures(events)
        return facts, insights, procedures, 0

    # -- nightly hygiene (heavier, once-per-local-day) ---------------------

    def hygiene_due(self, *, now: datetime) -> bool:
        """Whether the heavier nightly hygiene pass is due (first call of a new local day).

        arcmemory owns this decision, not arcagent: the poll heartbeat calls
        ``consolidate()`` and arcmemory escalates to hygiene the first time it runs after
        the local date rolls over. Tracked by a persisted stamp so an agent restart still
        fires at most once per local day.
        """
        last = self._read_hygiene_date()
        return last is None or last < self._local_date(now)

    async def run_hygiene(self, *, now: datetime | None = None) -> ConsolidationResult:
        """Run the light pass, then the day-level hygiene: merge + backlink repair + dedup.

        Every step is idempotent and file-driven (independent of the window), so running
        hygiene on a quiet day still reconciles the glass-box files. Stamps the hygiene
        date last so a same-day re-entry stays a light pass.
        """
        now = now or datetime.now(UTC)
        result = await self.run(now=now)
        self._merge_entities_deterministic()
        self._repair_backlinks()
        self._dedup_workspace()
        self._stamp_hygiene(now)
        return result

    def _merge_entities_deterministic(self) -> None:
        """Fold alias-related duplicate cards WITHOUT an embedder (closes the re-dup loop).

        ``merge_entities`` needs embeddings; this deterministic pre-pass folds any card
        whose slug matches a recorded alias of another card, so common variants collapse
        even on a model-less deployment. Graph edges follow the survivor.
        """
        index = self._semantic.aliases_index()
        for slug in self._semantic.slugs():
            owner = index.get(slug)
            if owner is None or owner == slug or self._semantic.read(owner) is None:
                continue
            if self._semantic.merge_into(owner, slug):
                self._graph.rename_node(self._scope.key, slug, owner)
                self._emit("memory.entity_merged", f"{slug}->{owner}")

    def _repair_backlinks(self) -> None:
        """Write reciprocal backlinks into every wiki-link target (bidirectional links)."""
        written = repair_backlinks(self._semantic)
        if written:
            self._emit("memory.backlinks_repaired", "graph", extra={"written": written})

    def _dedup_workspace(self) -> None:
        """Collapse any pre-canonicalization duplicate cards across the three stores."""
        report = dedup_workspace(self._workspace, apply=True)
        if report.groups:
            self._emit("memory.workspace_deduped", "memory", extra={"groups": report.groups})

    def _local_date(self, now: datetime) -> str:
        """The local calendar date (``YYYY-MM-DD``) for a UTC-aware instant."""
        return now.astimezone().date().isoformat()

    def _read_hygiene_date(self) -> str | None:
        """The local date hygiene last ran (None if it never has here)."""
        if not self._hygiene_last_path.exists():
            return None
        return self._hygiene_last_path.read_text(encoding="utf-8").strip() or None

    def _stamp_hygiene(self, now: datetime) -> None:
        """Persist the hygiene date so the once-per-local-day gate survives a restart."""
        self._hygiene_last_path.parent.mkdir(parents=True, exist_ok=True)
        self._hygiene_last_path.write_text(self._local_date(now), encoding="utf-8")

    # -- steps -------------------------------------------------------------

    async def _extract_facts(self, events: list[Event]) -> list[tuple[str, Fact]]:
        """Distill + apply facts; audit each mutation + the file it rewrote."""
        applied = await distill.extract_facts(
            events,
            distiller=self._distiller,
            store=self._semantic,
            config=self._cfg,
            embedder=self._embedder,
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

    async def merge_entities(self) -> list[tuple[str, str]]:
        """Confirm-gated de-dup: candidate clusters -> ONE LLM call -> fold only confirmed.

        The fix for identity drift, done safely: ``write_fact`` upserts by canonical slug
        only, so the distiller phrasing the same thing differently ("Austin, Texas" /
        "Austin, TX") minted separate cards. Here each same-type card's NAME is embedded and
        clustered at the WIDER ``entity_merge_candidate_threshold`` into CANDIDATE groups —
        *possible* duplicates, never merged on that alone. Each cluster of >= 2 goes to
        :meth:`EntityMergeConfirmer.confirm_entity_merges`, one bounded LLM call that
        conservatively returns the slug sub-groups that are the SAME real-world entity; only
        those fold, into the richest survivor (most facts, slug tie-break) via
        :meth:`SemanticStore.merge_into`, with graph edges repointed. Returns the
        ``(merged_from, merged_into)`` pairs.

        LOUD degrade (never a silent ``[]``): with no embedder wired, or candidates found
        but no confirmer wired, it emits a WARNING + a ``memory.dedup_skipped`` audit and
        merges nothing. A card with no similar same-type neighbor forms no cluster, so no
        LLM call is spent on it.
        """
        entities = [(s, e) for s in self._semantic.slugs() if (e := self._semantic.read(s))]
        if len(entities) < 2:
            return []
        embedded = await embed_or_none(self._embedder, [e.name for _, e in entities])
        if embedded is None:
            self._emit_dedup_skipped("no-embedder")
            return []
        vectors = {slug: vec for (slug, _), vec in zip(entities, embedded, strict=True)}

        by_type: dict[str, list[tuple[str, Entity]]] = defaultdict(list)
        for slug, entity in entities:
            by_type[entity.entity_type].append((slug, entity))

        clusters: list[list[tuple[str, Entity]]] = []
        for group in by_type.values():
            clusters += self._candidate_clusters(group, vectors)
        if not clusters:
            return []
        if self._confirmer is None:
            self._emit_dedup_skipped("no-confirmer")
            return []

        candidate_groups = [[self._entity_ref(s, e) for s, e in cluster] for cluster in clusters]
        confirmed = await self._confirmer.confirm_entity_merges(candidate_groups)
        return self._apply_confirmed_merges(confirmed, dict(entities))

    def _candidate_clusters(
        self, group: list[tuple[str, Entity]], vectors: dict[str, list[float]]
    ) -> list[list[tuple[str, Entity]]]:
        """Connected-components clustering of same-type cards by pairwise name-cosine.

        Two cards share a cluster when their name embeddings clear the wide candidate
        threshold (a *possible* duplicate). A card with no such neighbor forms no
        cluster and is dropped, so it never reaches the LLM confirmer.
        """
        threshold = self._cfg.entity_merge_candidate_threshold
        slugs = [slug for slug, _ in group]
        parent = {slug: slug for slug in slugs}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for a, b in combinations(slugs, 2):
            if _cosine(vectors[a], vectors[b]) >= threshold:
                parent[find(a)] = find(b)

        by_root: dict[str, list[tuple[str, Entity]]] = defaultdict(list)
        for slug, entity in group:
            by_root[find(slug)].append((slug, entity))
        return [members for members in by_root.values() if len(members) >= 2]

    def _entity_ref(self, slug: str, entity: Entity) -> distill.EntityRef:
        """Summarize a card as an ``EntityRef`` (slug + name + type + a few key facts)."""
        facts = [f"{f.predicate}: {f.value}" for f in entity.facts[:_ENTITY_REF_MAX_FACTS]]
        return distill.EntityRef(
            slug=slug, name=entity.name, entity_type=entity.entity_type, facts=facts
        )

    def _apply_confirmed_merges(
        self, confirmed: list[list[str]], by_slug: dict[str, Entity]
    ) -> list[tuple[str, str]]:
        """Fold each LLM-confirmed sub-group into its richest survivor; repoint edges."""
        merged: list[tuple[str, str]] = []
        for subgroup in confirmed:
            cards = [(slug, by_slug[slug]) for slug in subgroup if slug in by_slug]
            if len(cards) >= 2:
                merged += self._merge_entity_group(cards)
        return merged

    def _merge_entity_group(self, group: list[tuple[str, Entity]]) -> list[tuple[str, str]]:
        """Fold a CONFIRMED same-entity sub-group into its richest survivor (non-lossy)."""
        # Richest first (most facts, slug tie-break) so the survivor keeps the fullest card.
        group.sort(key=lambda se: (-len(se[1].facts), se[0]))
        survivor = group[0][0]
        merged: list[tuple[str, str]] = []
        for slug, _entity in group[1:]:
            if self._semantic.merge_into(survivor, slug):
                self._graph.rename_node(self._scope.key, slug, survivor)
                merged.append((slug, survivor))
        return merged

    def _emit_dedup_skipped(self, reason: str) -> None:
        """LOUD degrade for entity de-dup: WARNING log + a ``memory.dedup_skipped`` audit."""
        _log.warning("arcmemory entity de-dup skipped: %s", reason)
        self._emit("memory.dedup_skipped", "memory", extra={"reason": reason})

    async def _merge_entities_audited(self) -> None:
        """Run entity merge and audit each fold (part of the nightly hygiene)."""
        for merged_from, merged_into in await self.merge_entities():
            self._emit("memory.entity_merged", f"{merged_from}->{merged_into}")

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
