"""Consolidation — the slow "sleep" path that turns a day into durable memory.

This is the orchestrator (SDD 4.4, REQ-030/031/034). Off the hot path, over a
*bounded window* of the raw stream, it:

1. **extracts facts** (additive, `was:` trails, corroboration-grown confidence);
2. **mints insights** (the analogical abstractions, cues wired as graph nodes);
3. **promotes procedures** (action-sequences seen >= threshold — zero-LLM);
4. **records life events** — what happened in the USER's life (a meeting, a sale),
   each participant wired into the shared graph so a person is one hop from history;
5. **decays** unreinforced edges (salience-slowed, so a rare-but-vital edge lives);
6. **merges near-duplicate cues** to bound controlled-vocabulary drift (T-054);
7. **merges duplicate entity cards** — same-type name embeddings generate CANDIDATE
   clusters (a wide cosine bar), one bounded LLM call conservatively confirms which
   cards are the same real-world entity, and only the confirmed sub-groups fold. No
   merge is ever done on embedding similarity alone (a false merge is worse than a
   duplicate), and a card with no similar neighbor costs no LLM call;
8. **reindexes** the touched chunks so surface recall sees the new curated files.

Every mutation emits an ``AuditEvent`` to the injected sink (REQ-034), so the whole
cycle is reconstructable from a tamper-evident chain.

**Crash safety** (absorbing ``DeepConsolidator``'s write-ahead manifest): a
``in_progress`` marker is written *before* any file mutation and cleared only on
success. Because the curated markdown is truth and the SQLite index is disposable,
recovery from an interrupted run is simply "rebuild the index from the files that
did land, then clear the marker" — deterministic, no LLM, no partial state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Protocol, TypeVar

from arcprompt import PromptSource
from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from arctrust.identity import AgentIdentity
from arctrust.policy import PolicyPipeline

from arcmemory import distill
from arcmemory.agent_consolidate import AgenticResult, run_agentic_consolidation
from arcmemory.config import MemoryConfig
from arcmemory.curate import curate_for_distillation
from arcmemory.db import MemoryDB
from arcmemory.entity_dedup import EntityDeduper
from arcmemory.hygiene import dedup_workspace, repair_backlinks
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, IndexRebuilder, embed_or_none
from arcmemory.index.surface import SurfaceIndex, _cosine
from arcmemory.promotion.sweep import PromotionSweepResult
from arcmemory.react_adapter import ReactLoop, run_react_loop
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.events import EventStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import (
    ProceduralStore,
    apply_consolidated_steps,
    merge_procedures,
)
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tools import build_memory_tools
from arcmemory.types import (
    ConsolidationResult,
    DaySummary,
    Event,
    Fact,
    Insight,
    LifeEvent,
    Procedure,
    Scope,
    TimeWindow,
)

_log = logging.getLogger(__name__)

_T = TypeVar("_T")

_MANIFEST_NAME = ".consolidate-manifest.json"
_LAST_RUN_NAME = ".consolidate-last-run"
_HYGIENE_LAST_NAME = ".hygiene-last-run"
# The highest episodic ``seq`` already distilled on the automatic sleep path.
# Persisted so each run reads only NEW episodes instead of rescanning the whole
# stream — the difference between "catching up" and re-chewing all of history
# every cycle. ``-1`` (or an absent file) means nothing has been consolidated yet.
_WATERMARK_NAME = ".consolidate-watermark"
# Upper bound on batches drained in one nightly pass, so a huge or pathological
# backlog catches up over a couple of nights instead of running unbounded in one.
_MAX_NIGHTLY_BATCHES = 50
# Cosine at/above which two cue embeddings are treated as the same concept (T-054).
_CUE_MERGE_THRESHOLD = 0.92
# Key facts summarized onto an EntityRef for the LLM merge-confirmer (bounded input).
_ENTITY_REF_MAX_FACTS = 5
# Steps at or below this need no rewrite — a short card is already followable, and
# rewriting it would churn the operator's own wording for no gain.
_STEP_CONSOLIDATION_FLOOR = 10


class _PromotionRunner(Protocol):
    """The nightly promotion sweep as the consolidator sees it (SPEC-083 COMP-018)."""

    async def run(self, now: datetime) -> PromotionSweepResult: ...


def _with_promotion(
    total: ConsolidationResult, sweep: PromotionSweepResult | None, *, failed: bool
) -> ConsolidationResult:
    """Fold the sweep's outcome into the nightly result."""
    if failed:
        return total.model_copy(update={"promotion_status": "error"})
    if sweep is None:
        return total
    return total.model_copy(
        update={
            "promotion_status": sweep.status,
            "promotion_evaluated": sweep.evaluated,
            "promotion_promoted": sweep.promoted,
            "promotion_kept_private": sweep.kept_private,
            "promotion_blocked_secret": sweep.blocked_secret,
            "promotion_too_large": sweep.too_large,
            "promotion_deferred": sweep.deferred,
        }
    )


def _add_results(a: ConsolidationResult, b: ConsolidationResult) -> ConsolidationResult:
    """Sum two consolidation results field-wise (accumulate drained batches)."""
    return ConsolidationResult(
        facts_updated=a.facts_updated + b.facts_updated,
        insights_minted=a.insights_minted + b.insights_minted,
        procedures_promoted=a.procedures_promoted + b.procedures_promoted,
        events_recorded=a.events_recorded + b.events_recorded,
        days_summarized=a.days_summarized + b.days_summarized,
        edges_decayed=a.edges_decayed + b.edges_decayed,
        files_rewritten=a.files_rewritten + b.files_rewritten,
        window_events=a.window_events + b.window_events,
    )


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
        model_factory: Callable[[], object] | None = None,
        identity: AgentIdentity | None = None,
        policy_pipeline: PolicyPipeline | None = None,
        react_loop: ReactLoop = run_react_loop,
        store_raw_bodies: bool = False,
        promotion_sweep: _PromotionRunner | None = None,
        prompts: PromptSource | None = None,
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
        # Agentic-engine seams (the DEFAULT DISTILL path). Without a factory the
        # engine cannot run, so consolidation falls back to the pipeline distiller.
        # Held as a FACTORY so the loop's provider is built only when a
        # consolidation actually runs — startup must not require a provider key.
        self._model_factory = model_factory
        self._identity = identity
        self._policy = policy_pipeline
        self._react_loop = react_loop
        self._store_raw_bodies = store_raw_bodies
        # The agentic engine's system prompt source (agent overlay-aware, or stock).
        self._prompts = prompts
        # SPEC-083: runs last in the nightly pass; absent -> no promotion at all.
        self._promotion_sweep = promotion_sweep

        self._graph = WeightedGraph(db, self._cfg)
        self._semantic = SemanticStore(
            workspace,
            self._graph,
            scope=scope.key,
            fact_half_life_days=self._cfg.fact_half_life_days,
        )
        self._insights = InsightStore(workspace)
        self._procedures = ProceduralStore(workspace)
        self._events = EventStore(workspace)
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
        self._watermark_path = self._workspace / "memory" / _WATERMARK_NAME

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

    def watermark(self) -> int:
        """Highest episodic seq already distilled here (-1 if never)."""
        try:
            return int(self._watermark_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return -1

    def _stamp_watermark(self, seq: int) -> None:
        """Record the high-water seq so the next sleep reads only newer episodes.

        Written only after a run fully commits, so an interrupted run leaves the
        watermark behind and its episodes are re-read next time (at-least-once).
        """
        self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
        self._watermark_path.write_text(str(seq), encoding="utf-8")

    def _seed_watermark_from_last_run(self) -> None:
        """Gap-only catch-up: on a workspace that consolidated before this feature
        existed (a last-run stamp but no watermark), seed the watermark past
        everything already distilled, so the first run processes only the gap since
        the last successful consolidation — not the whole history. A fresh workspace
        (no last-run) is left at -1 so it still learns from all of its events.
        """
        if self._watermark_path.exists():
            return
        last = self.last_run()
        if last is None:
            return
        self._stamp_watermark(self._episodic.seq_at_or_before(self._scope.key, last.isoformat()))

    async def run(
        self, window: TimeWindow | None = None, *, now: datetime | None = None
    ) -> ConsolidationResult:
        """Run one bounded consolidation cycle; return its mutation counts.

        Automatic sleep (``window is None``) distills only episodes past the
        watermark, capped per run, and advances the watermark on success — so the
        backlog shrinks instead of the whole stream being re-read every cycle. An
        explicit window is a manual re-consolidation: it full-scans that range and
        leaves the watermark untouched.
        """
        now = now or datetime.now(UTC)
        if window is None:
            self._seed_watermark_from_last_run()
            raw_events, high_seq = self._episodic.events_since(
                self._scope.key,
                self.watermark(),
                limit=self._cfg.consolidate_max_events_per_run,
            )
            advance: int | None = high_seq
        else:
            all_events = self._episodic.events(self._scope.key)
            raw_events = [e for e in all_events if window.contains(e.ts)]
            advance = None
        # Distillation learns from the session CONVERSATION only — the user's turns and the
        # agent's responses. Tool frames and other machinery are dropped (curate.py), so the
        # LLM never distills the agent's own operational mechanics into facts/insights/methods.
        events = curate_for_distillation(raw_events, self._cfg)
        if not events:
            # No conversational episodes in this batch — still advance past the raw
            # events we read (they are curated out for good) so a batch of pure
            # machinery can never wedge the watermark and re-read forever.
            self._stamp_last_run(now)
            if advance is not None:
                self._stamp_watermark(advance)
            return ConsolidationResult()

        self._begin_manifest(len(events))
        facts, insights, procedures, life_events, agentic_writes = await self._distill(events)
        days = await self._summarize_days(events)
        decayed = self._decay(now)
        await self._merge_cues_audited()
        await self.merge_entities()
        await self.merge_duplicate_procedures()
        await self._surface.index_if_needed()
        self._commit_manifest()
        self._stamp_last_run(now)
        if advance is not None:
            self._stamp_watermark(advance)

        return ConsolidationResult(
            facts_updated=len(facts),
            insights_minted=len(insights),
            procedures_promoted=len(procedures),
            events_recorded=len(life_events),
            days_summarized=len(days),
            edges_decayed=decayed,
            files_rewritten=(
                len(facts)
                + len(insights)
                + len(procedures)
                + len(life_events)
                + len(days)
                + agentic_writes
            ),
            window_events=len(events),
        )

    # -- distill step: agentic engine (default) with pipeline fallback -----

    async def _distill(
        self, events: list[Event]
    ) -> tuple[list[tuple[str, Fact]], list[Insight], list[Procedure], list[LifeEvent], int]:
        """Route the DISTILL step: agentic engine by default, pipeline as fallback.

        Agentic mode runs the bounded ReAct loop over the memory tools (which write
        cards — facts, insights, procedures, and life events — directly). On a degrade
        (breach/timeout/arcrun-absent, or no model wired) the whole window is finished
        by the pipeline distiller so no data is lost.
        """
        if self._cfg.consolidate_engine == "agentic" and self._model_factory is not None:
            result = await self._run_agentic(events, self._model_factory())
            if not result.degraded:
                return [], [], [], [], result.tool_calls_made
            self._emit("memory.consolidation_degraded", result.reason or "degraded")
        return await self._distill_pipeline(events)

    async def _run_agentic(self, events: list[Event], model: object) -> AgenticResult:
        """Run one bounded agentic consolidation over this scope's memory tools.

        The model is passed in already built: the caller owns deciding whether the
        agentic engine runs at all, so this method never has to reason about a
        missing provider.
        """
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
            model=model,
            tools=tools,
            config=self._cfg,
            actor_did=actor_did,
            react_loop=self._react_loop,
            store_raw_bodies=self._store_raw_bodies,
            prompts=self._prompts,
        )

    async def _distill_pipeline(
        self, events: list[Event]
    ) -> tuple[list[tuple[str, Fact]], list[Insight], list[Procedure], list[LifeEvent], int]:
        """The deterministic single-shot distiller path (fallback + engine=pipeline).

        Each step is isolated: a malformed LLM response for one distiller (a bad
        insights or procedures payload) degrades that step to empty and the rest
        still run — and, crucially, ``run`` continues to daily notes. Left
        unisolated, one ValidationError aborted the whole sleep pass, so a single
        bad response cost the agent a full day of memory — no daily notes, no
        entities — which is exactly what "degrade, don't crash" forbids.
        """
        facts = await self._safe_distill("facts", self._extract_facts(events), [])
        insights = await self._safe_distill(
            "insights", self._mint_insights(events, [f for _, f in facts]), []
        )
        procedures = await self._safe_distill("procedures", self._extract_procedures(events), [])
        life_events = await self._safe_distill("events", self._extract_events(events), [])
        return facts, insights, procedures, life_events, 0

    async def _safe_distill(self, step: str, coro: Awaitable[_T], default: _T) -> _T:
        """Await one distill step, degrading to ``default`` on any failure.

        A distiller failure is a telemetry-worthy degrade, never a crash that
        loses the other steps and the daily notes downstream of it.
        """
        try:
            return await coro
        except Exception as exc:  # reason: one step must never abort the sleep pass
            _log.warning(
                "distill step %r degraded to empty (%s) — consolidation continues",
                step,
                type(exc).__name__,
            )
            self._emit("memory.distill_degraded", step)
            return default

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
        """The nightly pass: drain the whole backlog, then day-level hygiene.

        ``run`` distills one bounded batch and advances the watermark; the nightly
        pass loops it until the backlog is drained (a bounded number of batches, so a
        pathological stream can't run forever in one night) so the agent catches up in
        one night rather than one batch per night. Then the idempotent, file-driven
        hygiene — merge + backlink repair + dedup — reconciles the glass-box files.
        The promotion sweep runs after the files are reconciled, so it judges the
        merged cards. Stamps the hygiene date last so a same-day re-entry stays a
        light pass.
        """
        now = now or datetime.now(UTC)
        total = ConsolidationResult()
        for _ in range(_MAX_NIGHTLY_BATCHES):
            before = self.watermark()
            batch = await self.run(now=now)
            total = _add_results(total, batch)
            # Stop when a batch consumed no raw events (the watermark did not move),
            # NOT when it distilled nothing: a 500-event batch can be all tool
            # frames (zero conversation to distill) while real conversation still
            # waits deeper in the backlog — breaking on window_events==0 there
            # stranded the rest of the gap.
            if self.watermark() == before:
                break
        self._merge_entities_deterministic()
        self._repair_backlinks()
        self._dedup_workspace()
        result = await self._run_promotion(total, now)
        self._stamp_hygiene(now)
        return result

    async def _run_promotion(
        self, total: ConsolidationResult, now: datetime
    ) -> ConsolidationResult:
        """Run the promotion sweep, if composed; a sweep failure never aborts hygiene."""
        if self._promotion_sweep is None:
            return _with_promotion(total, None, failed=False)
        try:
            sweep = await self._promotion_sweep.run(now)
        except Exception as exc:  # reason: SDD COMP-018 — a sweep crash must not abort hygiene
            _log.warning("arcmemory promotion sweep failed", exc_info=True)
            self._emit(
                "memory.promotion.sweep_failed", "memory", extra={"error": type(exc).__name__}
            )
            return _with_promotion(total, None, failed=True)
        return _with_promotion(total, sweep, failed=False)

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
            if self._semantic.merge_into(owner, slug, strict=self._cfg.tier == "federal"):
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
        """Evolve the reusable how-to cards (LLM merge); audit each upsert + its file."""
        extracted = await distill.extract_procedures(
            events,
            distiller=self._distiller,
            store=self._procedures,
            config=self._cfg,
            graph=self._graph,
            scope=self._scope,
        )
        for procedure in extracted:
            self._emit("memory.procedure_extracted", procedure.slug)
            self._emit("memory.file_rewritten", str(self._procedures.path_for(procedure.slug)))
        return extracted

    async def _extract_events(self, events: list[Event]) -> list[LifeEvent]:
        """Distill what happened in the USER's life (LLM); audit each card + its file."""
        recorded = await distill.extract_events(
            events,
            distiller=self._distiller,
            store=self._events,
            graph=self._graph,
            scope=self._scope,
            config=self._cfg,
        )
        for event in recorded:
            self._emit("memory.event_recorded", event.slug)
            self._emit("memory.file_rewritten", str(self._events.path_for(event.slug)))
        return recorded

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
        embedded = await embed_or_none(self._embedder, cues, operation="embed:consolidate-cues")
        if embedded is None:
            return []
        vectors = dict(zip(cues, embedded, strict=True))
        # O(N^2) pure-Python cosine sweep — off the loop so a large store's nightly
        # "sleep" can't pin the single asyncio thread and starve NATS/websocket auth.
        canonical_of = await asyncio.to_thread(self._cluster_cues, cues, vectors)

        merges: list[tuple[str, str]] = []
        for cue, canonical in canonical_of.items():
            if cue == canonical:
                continue
            self._repoint_cue(cue, canonical)
            merges.append((cue, canonical))
        return merges

    async def merge_entities(self) -> list[tuple[str, str]]:
        """Identity de-dup over this scope's entity cards; returns ``(folded, survivor)``.

        Delegates to :class:`~arcmemory.entity_dedup.EntityDeduper` — the same engine
        ``arc memory dedup`` runs — so the nightly pass and the operator command can
        never disagree about what is a duplicate. Series-number pairs fold without a
        model; cross-type candidates (name tokens, plus embeddings when an embedder is
        wired) are LLM-confirmed; every fold is audited as ``memory.entity_merged``.
        """
        result = await self.entity_deduper().run(apply=True)
        return result.merged

    def entity_deduper(self) -> EntityDeduper:
        """The identity de-dup engine bound to this scope's store, graph and audit."""
        return EntityDeduper(
            self._semantic,
            self._graph,
            self._scope.key,
            config=self._cfg,
            embedder=self._embedder,
            confirmer=self._confirmer,
            emit=lambda action, target, extra: self._emit(action, target, extra=extra),
        )

    async def merge_duplicate_procedures(self) -> list[tuple[str, str]]:
        """Fold procedure cards that describe the SAME method into one.

        Procedures previously merged only when their filenames canonicalised to the
        same slug, so one method recorded under two titles stayed split forever —
        and a split procedure is worse than a split entity, because the agent
        follows whichever half it retrieves and the other half's steps never happen.

        Candidates are clustered on the TRIGGER (``when_to_use`` plus title): that is
        what a procedure is found by, and two cards answering the same situation are
        the same playbook however differently they are worded. The LLM confirms, as
        with entities — merging two genuinely different methods would put a step from
        one into the middle of the other.

        Returns the ``(folded, survivor)`` pairs. Degrades loudly: no embedder or no
        confirmer merges nothing and says so.
        """
        cards = [c for slug in self._procedures.slugs() if (c := self._procedures.read(slug))]
        if len(cards) < 2:
            return []
        triggers = [f"{c.title}. {c.when_to_use}" for c in cards]
        embedded = await embed_or_none(
            self._embedder, triggers, operation="embed:consolidate-procedure-dedup"
        )
        if embedded is None:
            self._emit_dedup_skipped("no-embedder-procedures")
            return []
        if self._confirmer is None:
            self._emit_dedup_skipped("no-confirmer-procedures")
            return []

        vectors = dict(zip([c.slug for c in cards], embedded, strict=True))
        by_slug = {c.slug: c for c in cards}
        # O(N^2) pure-Python cosine sweep — off the loop (see merge_cues).
        clusters = await asyncio.to_thread(self._procedure_clusters, cards, vectors)
        # Positive LLM confirmation, exactly as entity de-dup: the wide band only
        # NOMINATES candidates; the confirmer decides which are the same playbook and
        # declines the rest, so widening the band never fuses two real methods (a
        # "start" and an "update" procedure stay apart unless the model says they are one).
        merged: list[tuple[str, str]] = []
        if clusters:
            groups = [
                [
                    distill.EntityRef(
                        slug=c.slug,
                        name=c.title,
                        entity_type="procedure",
                        facts=[f"when_to_use: {c.when_to_use}"],
                    )
                    for c in cluster
                ]
                for cluster in clusters
            ]
            try:
                confirmed = await self._confirmer.confirm_entity_merges(groups)
            except Exception as exc:  # reason: never fuse two real methods on an error
                _log.warning("arcmemory procedure de-dup: confirm failed: %s", exc)
                self._emit_dedup_skipped("procedure-confirm-failed")
                confirmed = []
            for subgroup in confirmed:
                keep = [by_slug[s] for s in subgroup if s in by_slug]
                if len(keep) < 2:
                    continue
                # Richest first: the card with the most steps survives, so the merge adds
                # to the fuller method rather than rebuilding it from the thinner one.
                keep.sort(key=lambda c: (-len(c.steps), c.slug))
                survivor, folded = keep[0].slug, [c.slug for c in keep[1:]]
                card = merge_procedures(self._procedures, survivor=survivor, folded=folded)
                if card is None:
                    continue
                await self._consolidate_steps(card)
                for slug in folded:
                    self._graph.rename_node(self._scope.key, slug, survivor)
                    self._emit("memory.procedure_merged", f"{slug}->{survivor}")
                    merged.append((slug, survivor))
        self._emit(
            "memory.procedure_dedup_pass",
            "memory",
            extra={"procedures": len(by_slug), "clusters": len(clusters), "merged": len(merged)},
        )
        return merged

    async def _consolidate_steps(self, card: Procedure) -> None:
        """Collapse a merged card's repeated steps into one followable sequence.

        The fold is a union, so one method recorded eleven ways survives as a
        faithful 54-step card while the median card holds 6 — preserved, and far too
        long to follow. Only cards the union actually bloated are rewritten; a short
        card is already readable and rewriting it would churn the operator's wording
        for nothing.

        Fail-closed at every step. The rewrite is accepted only if it accounts for
        every original step exactly once (:func:`apply_consolidated_steps`), and any
        provider error keeps the union — a long procedure is a nuisance, a quietly
        shortened one no longer does what its author wrote.
        """
        if len(card.steps) <= _STEP_CONSOLIDATION_FLOOR:
            return
        consolidator = getattr(self._distiller, "consolidate_steps", None)
        if consolidator is None:
            return
        try:
            rewrite = await consolidator([step.text for step in card.steps])
        except Exception as exc:  # reason: never trade the method for a tidier one
            _log.warning("arcmemory: step consolidation failed, keeping the union: %s", exc)
            return
        steps = apply_consolidated_steps(card.steps, rewrite)
        if steps is None:
            self._emit(
                "memory.steps_consolidation_refused",
                card.slug,
                extra={"steps": len(card.steps), "proposed": len(rewrite)},
            )
            return
        card.steps = steps
        self._procedures.write(card)
        self._emit(
            "memory.steps_consolidated",
            card.slug,
            extra={"before": len(rewrite), "after": len(steps)},
        )

    def _procedure_clusters(
        self, cards: list[Procedure], vectors: dict[str, list[float]]
    ) -> list[list[Procedure]]:
        """Group procedures whose triggers embed close enough to be one method."""
        threshold = self._cfg.procedure_merge_candidate_threshold
        by_slug = {c.slug: c for c in cards}
        parent = {c.slug: c.slug for c in cards}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for a, b in combinations(list(by_slug), 2):
            if _cosine(vectors[a], vectors[b]) >= threshold:
                parent[find(a)] = find(b)

        grouped: dict[str, list[Procedure]] = defaultdict(list)
        for card in cards:
            grouped[find(card.slug)].append(card)
        return [members for members in grouped.values() if len(members) >= 2]

    def _emit_dedup_skipped(self, reason: str) -> None:
        """LOUD degrade for entity de-dup: WARNING log + a ``memory.dedup_skipped`` audit."""
        _log.warning("arcmemory entity de-dup skipped: %s", reason)
        self._emit("memory.dedup_skipped", "memory", extra={"reason": reason})

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
