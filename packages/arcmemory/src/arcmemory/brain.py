"""ArcMemoryBrain — the concrete ``Brain`` for arcagent's memory seam (SPEC-041 §4.2).

arcagent defines a *structural* ``Brain`` Protocol and a no-op ``NullBrain`` default;
it depends on **no** memory package. This class is the plug-in that satisfies that
Protocol structurally — it imports nothing from arcagent (the architecture test in
``tests/architecture`` guards that), speaking only in primitives (``str``/``int``) at
its edge and arcmemory's own types on the inside.

One brain per agent workspace. The three memory speeds are wired here over the four
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

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from arctrust.classification import parse_classification
from arctrust.identity import AgentIdentity
from arctrust.policy import PolicyPipeline

from arcmemory.capture import FastCapture
from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import Distiller
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, IndexRebuilder
from arcmemory.react_adapter import ReactLoop, run_react_loop
from arcmemory.retrieve import Retriever, attributed_cards
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.types import ConsolidationResult, Recall, RecallCard, Scope, Situation


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
        model_factory: Callable[[], object] | None = None,
        identity: AgentIdentity | None = None,
        policy_pipeline: PolicyPipeline | None = None,
        react_loop: ReactLoop = run_react_loop,
        store_raw_bodies: bool = False,
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
        # Agentic-consolidation seams (default engine); passed through to the
        # Consolidator. Without a factory the engine degrades to the pipeline
        # distiller. A FACTORY, not a model: the loop's provider is built when a
        # consolidation runs, so memory costs no provider key at startup.
        self._model_factory = model_factory
        self._identity = identity
        self._policy = policy_pipeline
        self._react_loop = react_loop
        self._store_raw_bodies = store_raw_bodies
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
        self._emit_recall_attribution(result.recalls)
        return result.text

    async def recall(
        self,
        query: str,
        *,
        clearance: str = "unclassified",
        top_k: int = 5,
        budget: int = 1024,
        summary: str = "",
        cues: list[str] | None = None,
        session_id: str | None = None,
    ) -> list[RecallCard]:
        """Structured glass-box recall — ranked cards WITH provenance + ``[[links]]``.

        The first-class ``recall`` the agent-side tool surfaces: same fast, gated,
        bounded pass as :meth:`retrieve` (retrieval is NOT agentic), but returns the
        typed cards instead of the injectable text, so a caller can see WHERE each
        memory came from and WHAT it points to. Never raises on a missing embedder.
        """
        bundle = self._bundle(session_id)
        await bundle.retriever.index()
        clr = parse_classification(clearance, strict=self._cfg.tier == "federal")
        situation = Situation(text=query, summary=summary, cues=list(cues or []))
        return await bundle.retriever.recall_cards(
            situation, clearance=clr, top_k=top_k, budget=budget
        )

    async def holdings(self, *, limit: int = 200, session_id: str | None = None) -> list[str]:
        """Publishable pointers to durable knowledge this memory holds (no bodies).

        Each semantic entity card is keyed by the proper noun that names it —
        exactly the signal a channel router needs to find the agent that holds a
        topic, without waking anyone. Only **unclassified** cards surface: a digest
        crosses to teammates as pointers, so a classified holding must never appear.
        Returns one short ``name — predicate: value`` line per card; the caller
        reduces it to a title plus proper nouns, and no body beyond that line ever
        leaves memory.
        """
        from arcmemory.stores.semantic import SemanticStore

        store = SemanticStore(self._workspace, self._graph, self._scope(session_id).key)
        lines: list[str] = []
        for slug in store.slugs()[:limit]:
            entity = store.read(slug)
            if entity is None or entity.classification != "unclassified":
                continue
            fact = entity.facts[0] if entity.facts else None
            line = " ".join(p for p in (entity.name, *entity.aliases, *entity.tags) if p).strip()
            if fact is not None:
                detail = f"{fact.predicate}: {fact.value}".strip()
                line = f"{line} — {detail}" if line else detail
            if line:
                lines.append(line)
        return lines

    async def consolidate(self, *, session_id: str | None = None) -> Mapping[str, object]:
        """Slow "sleep" consolidation over the raw stream (REQ-030..034).

        Returns the mutation counts plus a human-readable ``episode_summary`` used
        to ground reflection (SPEC-041 Phase 9). A brain with no distiller cannot
        distill facts/insights, so it returns an empty result rather than erroring.

        arcmemory owns the cadence, not arcagent: the caller's poll heartbeat invokes
        this every trigger; arcmemory decides internally which pass to run. The first
        call after the local date rolls over escalates to the heavier nightly hygiene
        pass (full merge + backlink repair + dedup); otherwise the light per-interval
        consolidation runs at most once per ``consolidate_interval_minutes`` (default
        60), and a call inside both windows is a no-op.
        """
        consolidator = self._bundle(session_id).consolidator
        if consolidator is None:
            return self._summarize(ConsolidationResult())
        if consolidator.pending_recovery:
            await consolidator.recover()
        now = datetime.now(UTC)
        if consolidator.hygiene_due(now=now):
            return self._summarize(await consolidator.run_hygiene(now=now))
        if consolidator.due(now=now, interval_minutes=self._cfg.consolidate_interval_minutes):
            return self._summarize(await consolidator.run(now=now))
        return self._summarize(ConsolidationResult())

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

    def _emit_recall_attribution(self, recalls: list[Recall]) -> None:
        """Record WHICH cards a recall surfaced, not merely that one happened.

        Memory audited that a recall occurred and whether it returned anything — never
        what it returned — so nothing downstream could ask whether surfacing a given
        card actually helped. That question is the entire basis for improving retrieval
        over time, and it cannot be asked retroactively: the attribution has to be
        written at the moment the bundle is built.

        Cards only, and deduplicated. Per-chunk credit starves on a real store (1,528
        indexed chunks against a handful of turns an hour), which is why the unit is
        the card an operator edits and consolidation merges.
        """
        cards = attributed_cards(recalls)
        if not cards:
            return
        emit(
            AuditEvent(
                actor_did=self._scope(None).agent_did,
                action="memory.recall_attributed",
                target="memory",
                outcome="allow",
                extra={"cards": cards},
            ),
            self._audit,
        )

    async def list_procedures(self, *, session_id: str | None = None) -> str:
        """Every playbook's slug + trigger + counters, WITHOUT its steps.

        The index an agent scans to ask "is there already a way we do this?" — cheap
        enough to afford on any turn, which is the whole point: the full step lists of
        a mature store do not fit, so the alternative to this listing is not reading
        them all, it is never consulting them.
        """
        store = ProceduralStore(self._workspace)
        summaries = store.list_summaries()
        if not summaries:
            return "(no procedures recorded)"
        return "\n".join(
            f"- {s.slug} | {s.title} | when_to_use: {s.when_to_use} "
            f"(used {s.use_count}x, revised {s.revisions}x)"
            for s in summaries
        )

    async def get_procedure(self, slug: str, *, session_id: str | None = None) -> str:
        """One playbook in full, and record the use.

        Reading IS the use — it is the only point at which the system can learn which
        playbooks earn their keep. ``use_count`` previously moved only on writes, so a
        live store held 36 procedures and no evidence that any had been reached for.
        """
        store = ProceduralStore(self._workspace)
        procedure = store.read(slug)
        if procedure is None:
            return f"(no procedure {slug!r})"
        store.increment_use(procedure.slug)
        # Corroboration is shown per step so the reader can tell the operator's firm
        # practice from something said once — an unmarked list claims every step is
        # equally settled, which is exactly what it cannot know.
        steps = "\n".join(
            f"{i}. {step.text}  [{step.hits}x corroborated]"
            for i, step in enumerate(procedure.steps, start=1)
        )
        return (
            f"{procedure.title}\nwhen_to_use: {procedure.when_to_use}\n"
            f"(used {procedure.use_count + 1}x, revised {procedure.revisions}x)\n{steps}"
        )

    async def authorize(self, operation: str, *, caller_did: str = "") -> bool:
        """Provider-side ACL gate the host's generic memory adapter consults per op.

        The brain is bound to one agent that owns its store, so the bound agent (and the
        empty caller of unit contexts) is always authorized to capture/recall/search its
        own memory. Cross-session visibility of OTHER sessions' memory is a separate
        concern gated internally by :mod:`arcmemory.acl` when arcmemory surfaces them.

        A denial is audited (AU-2) so every ACL decision remains tamper-evident, keeping
        the audit property the removed arcagent memory_acl hook used to provide.
        """
        allowed = not caller_did or caller_did == self._agent_did
        if not allowed:
            emit(
                AuditEvent(
                    actor_did=caller_did,
                    action="memory.acl.denied",
                    target=operation,
                    outcome="deny",
                    tier=self._cfg.tier,
                    extra={"owner_did": self._agent_did},
                ),
                self._audit,
            )
        return allowed

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
                confirmer=self._distiller,
                seed_vocabulary=self._seed_vocab,
                model_factory=self._model_factory,
                identity=self._identity,
                policy_pipeline=self._policy,
                react_loop=self._react_loop,
                store_raw_bodies=self._store_raw_bodies,
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
