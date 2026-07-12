"""ArcMemoryBrain — the concrete ``Brain`` for arcagent's memory seam (SPEC-041 §4.2).

arcagent defines a *structural* ``Brain`` Protocol and a no-op ``NullBrain`` default;
it depends on **no** memory package. This class is the plug-in that satisfies that
Protocol structurally — it imports nothing from arcagent (the architecture test in
``tests/architecture`` guards that), speaking only in primitives (``str``/``int``) at
its edge and arcmemory's own types on the inside.

One brain per agent workspace. The three FERNme speeds are wired here over the four
stores + two indices:

* ``capture``     → :class:`~arcmemory.capture.FastCapture` (fast, zero-LLM);
* ``retrieve``    → :class:`~arcmemory.retrieve.Retriever` (single-pass, gated, bounded);
* ``consolidate`` → :class:`~arcmemory.consolidate.Consolidator` (slow, LLM sleep path).

The embedder and distiller are injected seams (never imported here). With neither
present the brain still runs: capture is zero-LLM regardless; recall degrades to
BM25 + graph (``recall.degraded``); consolidation without a distiller is a no-op. A
deployment wires arcllm-backed seams to light up semantic recall and distillation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from arctrust.audit import AuditSink, NullSink
from arctrust.classification import parse_classification

from arcmemory.capture import FastCapture
from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import Distiller
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, IndexRebuilder
from arcmemory.retrieve import Retriever
from arcmemory.types import ConsolidationResult, Scope, Situation


class _ScopeBundle:
    """The per-scope capture/retrieve/consolidate helpers, built once and reused."""

    __slots__ = ("capture", "consolidator", "retriever")

    def __init__(
        self,
        capture: FastCapture,
        retriever: Retriever,
        consolidator: Consolidator | None,
    ) -> None:
        self.capture = capture
        self.retriever = retriever
        self.consolidator = consolidator


class ArcMemoryBrain:
    """arcmemory's implementation of arcagent's structural ``Brain`` seam.

    Bound to one ``agent_did`` + workspace at construction; a per-call
    ``session_id`` narrows the scope (shared-nothing isolation, LLM08). The
    embedder/distiller seams are optional — see the module docstring for the
    degrade behavior.
    """

    def __init__(
        self,
        workspace: Path | str,
        agent_did: str,
        *,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        distiller: Distiller | None = None,
        audit_sink: AuditSink | None = None,
        seed_vocabulary: Iterable[str] | None = None,
    ) -> None:
        if not agent_did:
            raise ValueError("ArcMemoryBrain requires an agent_did (no memory without identity)")
        self._workspace = Path(workspace)
        self._agent_did = agent_did
        self._cfg = config or MemoryConfig()
        self._embedder = embedder
        self._distiller = distiller
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._seed_vocab = list(seed_vocabulary or [])
        self._db = MemoryDB(self._workspace)
        self._graph = WeightedGraph(self._db, self._cfg)
        self._bundles: dict[str, _ScopeBundle] = {}

    # -- Brain Protocol ----------------------------------------------------

    async def capture(
        self,
        text: str,
        *,
        kind: str = "observation",
        salience: float = 0.0,
        classification: str = "unclassified",
        session_id: str | None = None,
    ) -> None:
        """Fast, zero-LLM capture of one untrusted text (REQ-010/011/012)."""
        self._bundle(session_id).capture.capture(
            text, kind=kind, salience=salience, classification=classification
        )

    async def retrieve(
        self,
        query: str,
        *,
        clearance: str = "unclassified",
        top_k: int = 5,
        budget: int = 1024,
        summary: str = "",
        cues: list[str] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Single-pass, clearance-gated, boundary-marked recall (REQ-040..062).

        ``summary`` is the turn's already-computed abstraction (reused, no new LLM call
        — OQ-1); it drives the analogical trigger channel and the cue tagging. ``cues``
        are the turn's active concept/entity nodes (graph seeds) for the structural
        cue channel — when omitted, arcmemory derives them by tagging the abstraction
        against its own entity/cue graph. Both are optional (backward-compatible).

        Returns the injectable ``<memory-result>`` rendering (empty string when
        nothing survives the gate). Never raises on a missing embedder — recall
        degrades to BM25 + graph.
        """
        bundle = self._bundle(session_id)
        await bundle.retriever.index()
        clr = parse_classification(clearance, strict=self._cfg.tier == "federal")
        situation = Situation(text=query, summary=summary, cues=list(cues or []))
        result = await bundle.retriever.retrieve(
            situation, clearance=clr, top_k=top_k, budget=budget
        )
        return result.text

    async def consolidate(self, *, session_id: str | None = None) -> Mapping[str, object]:
        """Slow "sleep" consolidation over the raw stream (REQ-030..034).

        Returns the mutation counts plus a human-readable ``episode_summary`` used
        to ground reflection (SPEC-041 Phase 9). A brain with no distiller cannot
        distill facts/insights, so it returns an empty result rather than erroring.

        Cadence-gated: the slow LLM sleep-path runs at most once per
        ``consolidate_interval_minutes`` (default 60), so a per-turn call within
        that window is a no-op rather than a fresh distillation.
        """
        consolidator = self._bundle(session_id).consolidator
        if consolidator is None:
            return self._summarize(ConsolidationResult())
        if consolidator.pending_recovery:
            await consolidator.recover()
        now = datetime.now(UTC)
        if not consolidator.due(now=now, interval_minutes=self._cfg.consolidate_interval_minutes):
            return self._summarize(ConsolidationResult())
        result = await consolidator.run(now=now)
        return self._summarize(result)

    async def rebuild_index(self, *, session_id: str | None = None) -> None:
        """Re-derive the disposable indices from the glass-box files + stream (REQ-022)."""
        await IndexRebuilder(
            self._db,
            self._workspace,
            self._scope(session_id),
            config=self._cfg,
            embedder=self._embedder,
            seed_vocabulary=self._seed_vocab,
        ).rebuild()

    # -- internals ---------------------------------------------------------

    def _scope(self, session_id: str | None) -> Scope:
        return Scope(agent_did=self._agent_did, session_id=session_id)

    def _bundle(self, session_id: str | None) -> _ScopeBundle:
        scope = self._scope(session_id)
        cached = self._bundles.get(scope.key)
        if cached is not None:
            return cached
        capture = FastCapture(
            self._db,
            self._workspace,
            scope,
            self._graph,
            config=self._cfg,
            audit_sink=self._audit,
            seed_vocabulary=self._seed_vocab,
        )
        retriever = Retriever(
            self._db,
            self._workspace,
            scope,
            config=self._cfg,
            embedder=self._embedder,
            audit_sink=self._audit,
            seed_vocabulary=self._seed_vocab,
        )
        consolidator = (
            Consolidator(
                self._db,
                self._workspace,
                scope,
                distiller=self._distiller,
                config=self._cfg,
                audit_sink=self._audit,
                embedder=self._embedder,
                seed_vocabulary=self._seed_vocab,
            )
            if self._distiller is not None
            else None
        )
        bundle = _ScopeBundle(capture, retriever, consolidator)
        self._bundles[scope.key] = bundle
        return bundle

    @staticmethod
    def _summarize(result: ConsolidationResult) -> dict[str, object]:
        summary = (
            f"Consolidation: {result.facts_updated} fact(s) updated, "
            f"{result.insights_minted} insight(s) minted, "
            f"{result.procedures_promoted} procedure(s) promoted, "
            f"{result.days_summarized} day(s) summarized, "
            f"{result.edges_decayed} edge(s) decayed over {result.window_events} event(s)."
        )
        data = dict(result.model_dump())
        data["episode_summary"] = summary
        return data


__all__ = ["ArcMemoryBrain"]
