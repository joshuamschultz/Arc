"""T-060..T-065 — structural / analogical retrieval (the centerpiece).

These tests prove the *differentiating* claim: a present situation is matched to a
past abstraction whose relation is **structural, not lexical** — zero surface
overlap with the episodes the insight generalizes.

The embedder is a deterministic **concept** embedder (mirrors ``test_surface``): it
maps text to an abstraction-space dimension by the presence of a mechanism marker,
so two *abstractions* that name the same mechanism land near each other while
sharing no token with the raw domain episodes. That is what lets these tests be
reproducible without a live model — the structural relationship is encoded in a
controlled embedding, not inferred by a network call.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.structural import StructuralIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Confidence, Event, Insight, Scope, Situation

# Abstraction-space concepts: each marker set is a *mechanism*, not a domain word.
# No marker appears in any raw domain episode below (asserted in the tests), so a
# concept hit can only come from an abstraction, never from surface leakage.
_CONCEPTS: dict[int, set[str]] = {
    0: {"asserted", "unwired", "declared", "uninvoked"},  # "guarantee never connected"
    1: {"orphaned", "subscribes", "dangling"},  # "listener without emitter"
    2: {"starvation", "unbounded", "runaway"},  # distractor mechanism
}
_DIMS = 8


class ConceptEmbedder:
    """Deterministic abstraction-space embedder over a fixed mechanism lexicon."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vec = [0.0] * _DIMS
            for dim, words in _CONCEPTS.items():
                if any(w in lowered for w in words):
                    vec[dim] = 1.0
            out.append(vec)
        return out


def _structural(
    db: MemoryDB, workspace: Path, scope: Scope, *, embedder: ConceptEmbedder | None = None
) -> StructuralIndex:
    return StructuralIndex(db, workspace, scope, embedder=embedder)


def _mint(
    workspace: Path,
    db: MemoryDB,
    scope: Scope,
    insight: Insight,
    *,
    salience: float = 0.0,
    ts: str | None = None,
) -> None:
    """Write an insight card and wire its cue edges (as ``distill.mint_insights`` does)."""
    InsightStore(workspace).write(insight)
    graph = WeightedGraph(db)
    for cue in insight.cues:
        graph.link(scope.key, insight.id, cue, kind="cue", ts=ts)
        if salience > 0.0:
            graph.hebbian_bump(
                scope.key, insight.id, cue, kind="cue", directed=True, salience=salience, ts=ts
            )


def _no_token_overlap(a: str, b_texts: list[str]) -> bool:
    """True when ``a`` shares no salient (len>3) token with any of ``b_texts``."""
    a_tokens = {t for t in a.lower().replace("-", " ").split() if len(t) > 3}
    for b in b_texts:
        b_tokens = {t for t in b.lower().replace("-", " ").split() if len(t) > 3}
        if a_tokens & b_tokens:
            return False
    return True


# -- T-060: trigger index kept SEPARATE from surface vec0 -------------------


async def test_trigger_index_embeds_triggers_apart_from_surface(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    _mint(
        workspace,
        db,
        scope,
        Insight(id="p1", statement="s", trigger="asserted guarantee left unwired", cues=["c-a"]),
    )
    _mint(
        workspace,
        db,
        scope,
        Insight(
            id="p2", statement="s", trigger="orphaned handler subscribes to nothing", cues=["c-b"]
        ),
    )

    indexed = await _structural(db, workspace, scope, embedder=emb).trigger_index()
    assert indexed == 2  # both triggers embedded

    conn = db.connect()
    trig_ids = {r[0] for r in conn.execute("SELECT insight_id FROM insight_trigger").fetchall()}
    assert trig_ids == {"p1", "p2"}
    # The trigger vectors live in their OWN table — NOT commingled with surface vec0.
    if db.vec_available:
        vec_keys = {r[0] for r in conn.execute("SELECT chunk_id FROM vec0").fetchall()}
        assert not (trig_ids & vec_keys)


async def test_trigger_index_is_content_gated(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    _mint(workspace, db, scope, Insight(id="p1", statement="s", trigger="asserted yet unwired"))
    idx = _structural(db, workspace, scope, embedder=emb)

    assert await idx.trigger_index() == 1
    assert emb.calls == 1
    # Nothing changed -> no re-embed on the second pass.
    assert await idx.trigger_index() == 0
    assert emb.calls == 1


async def test_trigger_index_without_embedder_is_noop(workspace, db, scope) -> None:
    _mint(workspace, db, scope, Insight(id="p1", statement="s", trigger="asserted yet unwired"))
    assert await _structural(db, workspace, scope, embedder=None).trigger_index() == 0


# -- T-061: channel (a) trigger-embedding, mechanism-level match ------------


async def test_trigger_match_finds_insight_with_no_surface_overlap(workspace, db, scope) -> None:
    """A mechanism-level situation matches a trigger sharing NO token with the episodes."""
    episodes = [
        "the recipe lists salt but the cook forgets it entirely",
        "the checklist names a valve the operator never rotates",
    ]
    insight = Insight(
        id="silent-noop",
        statement="A guarantee is claimed but its enforcement is never connected.",
        trigger="a property is asserted yet the enforcing mechanism stays unwired",
        cues=["claims-without-enforcement"],
    )
    # The trigger is a genuine abstraction: no shared token with any raw episode.
    assert _no_token_overlap(insight.trigger, episodes)
    _mint(workspace, db, scope, insight)

    emb = ConceptEmbedder()
    idx = _structural(db, workspace, scope, embedder=emb)
    await idx.trigger_index()

    # A DIFFERENT-domain situation, abstracted to the mechanism level.
    situation = Situation(
        text="the fee schedule promises a cap that no ledger ever applies",
        summary="a safeguard is declared but left uninvoked on the live path",
    )
    assert _no_token_overlap(situation.text, episodes)

    ranked = await idx.trigger_match(situation, top_k=3)
    assert ranked is not None
    assert ranked[0][0] == "silent-noop"


async def test_trigger_match_without_embedder_returns_none(workspace, db, scope) -> None:
    _mint(workspace, db, scope, Insight(id="p1", statement="s", trigger="asserted yet unwired"))
    idx = _structural(db, workspace, scope, embedder=None)
    assert await idx.trigger_match(Situation(text="x", summary="asserted")) is None


# -- T-062: channel (b) cue-graph spreading activation ----------------------


def test_cue_match_retrieves_with_zero_overlap_to_instances(workspace, db, scope) -> None:
    """Lighting the situation's cues retrieves the insight with ZERO overlap to instances."""
    instances = [
        Event(event_id="e0", scope=scope.key, kind="obs", text="the recipe omits the salt step"),
        Event(event_id="e1", scope=scope.key, kind="obs", text="the operator skips the valve"),
    ]
    cues = ["claims-without-enforcement"]
    # The cue label is abstraction-space: it appears in no instance (not leakage).
    assert _no_token_overlap(cues[0], [e.text for e in instances])

    _mint(
        workspace,
        db,
        scope,
        Insight(
            id="silent-noop",
            statement="s",
            trigger="asserted yet unwired",
            cues=cues,
            instances=["e0", "e1"],
        ),
    )

    idx = _structural(db, workspace, scope)
    # The current situation shares NO surface token with the instances — only the
    # abstraction-space cue bridges them.
    situation = Situation(text="a totally unrelated finance workflow", cues=cues)
    assert _no_token_overlap(situation.text, [e.text for e in instances])

    ranked = idx.cue_match(situation, top_k=3)
    assert ranked, "cue-graph spreading must retrieve the insight"
    assert ranked[0][0] == "silent-noop"


def test_cue_match_empty_when_no_cues_light(workspace, db, scope) -> None:
    _mint(
        workspace,
        db,
        scope,
        Insight(id="p1", statement="s", trigger="t", cues=["c-a"]),
    )
    idx = _structural(db, workspace, scope)
    assert idx.cue_match(Situation(text="nothing", cues=["c-unrelated"])) == []


def test_cue_match_tags_cues_from_situation_text(workspace, db, scope) -> None:
    """When the situation carries no explicit cues, they are tagged from its text."""
    _mint(
        workspace,
        db,
        scope,
        Insight(id="silent-noop", statement="s", trigger="t", cues=["budget-breaker"]),
    )
    idx = _structural(db, workspace, scope)
    # No explicit cues — the phrase in the summary lights the cue node itself.
    ranked = idx.cue_match(Situation(text="x", summary="the budget breaker never fires"))
    assert ranked and ranked[0][0] == "silent-noop"


# -- degrade: no embedder -> cue-graph channel only -------------------------


async def test_match_degrades_to_cue_graph_without_embedder(workspace, db, scope) -> None:
    from arctrust.audit import AuditEvent

    class RecordingSink:
        def __init__(self) -> None:
            self.events: list[AuditEvent] = []

        def write(self, event: AuditEvent) -> None:
            self.events.append(event)

    sink = RecordingSink()
    _mint(
        workspace,
        db,
        scope,
        Insight(id="silent-noop", statement="s", trigger="t", cues=["c-a"]),
    )
    idx = StructuralIndex(db, workspace, scope, embedder=None, audit_sink=sink)

    result = await idx.match(Situation(text="x", cues=["c-a"]))
    # Trigger channel is gone; the cue-graph channel alone still retrieves.
    assert result.degraded is True
    assert result.recalls and result.recalls[0].source == "silent-noop"
    assert any(e.action == "recall.degraded" for e in sink.events)


# -- T-063: confidence gate (known vs guessed; guessed decays out) ----------


async def test_confidence_gate_flags_guessed_not_known(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    known = Insight(
        id="known-p",
        statement="s",
        trigger="asserted yet unwired guarantee",
        cues=["c-known"],
        confidence=0.9,
        status=Confidence.KNOWN,
    )
    guessed = Insight(
        id="guessed-p",
        statement="s",
        trigger="orphaned handler that subscribes",
        cues=["c-guessed"],
        confidence=0.4,
        status=Confidence.GUESSED,
    )
    _mint(workspace, db, scope, known)
    _mint(workspace, db, scope, guessed)
    idx = _structural(db, workspace, scope, embedder=emb)
    await idx.trigger_index()

    known_hit = await idx.match(
        Situation(text="x", summary="a declared safeguard left uninvoked", cues=["c-known"])
    )
    assert known_hit.recalls[0].source == "known-p"
    assert known_hit.recalls[0].verify_first is False  # actionable anchor

    guessed_hit = await idx.match(
        Situation(text="y", summary="a dangling orphaned subscription", cues=["c-guessed"])
    )
    assert guessed_hit.recalls[0].source == "guessed-p"
    assert guessed_hit.recalls[0].verify_first is True  # surfaced tentatively


async def test_never_recurring_guessed_insight_decays_out(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    t0 = "2026-01-01T00:00:00+00:00"
    guessed = Insight(
        id="guessed-p",
        statement="s",
        trigger="orphaned handler that subscribes",
        cues=["c-guessed"],
        status=Confidence.GUESSED,
    )
    _mint(workspace, db, scope, guessed, ts=t0)  # salience 0, minted once, never reinforced
    idx = _structural(db, workspace, scope, embedder=emb)
    await idx.trigger_index()

    situation = Situation(text="y", summary="a dangling orphaned subscription", cues=["c-guessed"])
    # Fresh: matched via BOTH channels.
    assert (await idx.match(situation)).recalls[0].source == "guessed-p"

    # Simulate time: the nightly decay runs with no reinforcement -> cue edge forgotten.
    later = datetime.fromisoformat(t0) + timedelta(days=60)
    WeightedGraph(db).decay(scope.key, now=later)

    # Channel (b) now yields nothing -> conjunctive gate drops it. It decayed out.
    assert (await idx.match(situation)).recalls == []


# -- T-064: enrichment — spot, then enrich ----------------------------------


def test_enrich_bundles_instances_neighbors_and_stream(workspace, db, scope) -> None:
    episodic = EpisodicStore(db, workspace)
    stream = [
        Event(
            event_id="before",
            scope=scope.key,
            kind="obs",
            text="unrelated preamble",
            ts="2026-01-01T00:00:00+00:00",
        ),
        Event(
            event_id="e0",
            scope=scope.key,
            kind="obs",
            text="the recipe omits salt about ada",
            ts="2026-01-01T00:00:01+00:00",
        ),
        Event(
            event_id="mid",
            scope=scope.key,
            kind="obs",
            text="unrelated middle",
            ts="2026-01-01T00:00:02+00:00",
        ),
        Event(
            event_id="e1",
            scope=scope.key,
            kind="obs",
            text="the operator skips the valve",
            ts="2026-01-01T00:00:03+00:00",
        ),
        Event(
            event_id="after",
            scope=scope.key,
            kind="obs",
            text="unrelated tail",
            ts="2026-01-01T00:00:04+00:00",
        ),
    ]
    for ev in stream:
        episodic.append(ev)

    # A related entity mentioned inside an instance.
    SemanticStore(workspace, WeightedGraph(db), scope=scope.key).write_fact(
        "ada", "role", "engineer", confidence=0.7
    )

    _mint(
        workspace,
        db,
        scope,
        Insight(
            id="silent-noop",
            statement="s",
            trigger="t",
            cues=["shared-cue"],
            instances=["e0", "e1"],
        ),
    )
    # An adjacent insight sharing the same cue node.
    _mint(
        workspace,
        db,
        scope,
        Insight(id="cousin", statement="s", trigger="t2", cues=["shared-cue"]),
    )

    bundle = _structural(db, workspace, scope).enrich("silent-noop")

    assert {e.event_id for e in bundle.instances} == {"e0", "e1"}
    assert "ada" in {ent.slug for ent in bundle.entities}
    assert "cousin" in {i.id for i in bundle.adjacent_insights}
    ctx_ids = {e.event_id for e in bundle.stream_context}
    assert {"before", "mid", "after"} <= ctx_ids  # N-hop stream neighbors of the instances


async def test_match_folds_enrichment_into_recall_content(workspace, db, scope) -> None:
    """The production path (match -> _to_recall) returns ENRICHED content, not the bare
    statement — instances, adjacent insights, and related entities all reach the recall."""
    episodic = EpisodicStore(db, workspace)
    episodic.append(
        Event(event_id="e0", scope=scope.key, kind="obs", text="the recipe omits salt about ada")
    )
    SemanticStore(workspace, WeightedGraph(db), scope=scope.key).write_fact(
        "ada", "role", "engineer", confidence=0.7
    )
    _mint(
        workspace,
        db,
        scope,
        Insight(
            id="silent-noop",
            statement="A guarantee is claimed but never enforced.",
            trigger="t",
            cues=["shared-cue"],
            instances=["e0"],
        ),
    )
    _mint(
        workspace,
        db,
        scope,
        Insight(id="cousin", statement="A neighbouring abstraction.", trigger="t2",
                cues=["shared-cue"]),
    )

    # No embedder -> the cue-graph channel alone promotes it (degraded), exercising the
    # exact _to_recall enrichment fold that the fused/embedded path also runs.
    result = await _structural(db, workspace, scope).match(
        Situation(text="unrelated", cues=["shared-cue"])
    )
    recall = next(r for r in result.recalls if r.source == "silent-noop")
    assert "A guarantee is claimed but never enforced." in recall.content  # statement anchor
    assert "the recipe omits salt about ada" in recall.content  # instance folded in
    assert "A neighbouring abstraction." in recall.content  # adjacent insight folded in
    assert "Related: Ada" in recall.content  # related entity card folded in


# -- T-065: optional rerank, tier-gated + margin fallback -------------------


class RecordingReranker:
    """Injected cross-encoder stub — records calls, reverses order to prove it ran."""

    def __init__(self) -> None:
        self.calls = 0
        self.batch_sizes: list[int] = []

    async def rerank(self, situation: str, candidates: list[str]) -> list[float]:
        self.calls += 1
        self.batch_sizes.append(len(candidates))
        # Descending scores in input order -> a no-op ordering (deterministic).
        return [float(len(candidates) - i) for i in range(len(candidates))]


async def _two_insight_index(
    workspace: Path, db: MemoryDB, scope: Scope, tier: str
) -> tuple[StructuralIndex, ConceptEmbedder]:
    emb = ConceptEmbedder()
    # Two KNOWN insights on the SAME trigger concept but DISTINCT cues, so which cues
    # the situation lights controls the candidate-set size.
    _mint(
        workspace,
        db,
        scope,
        Insight(
            id="p1",
            statement="s",
            trigger="asserted yet unwired",
            cues=["c1"],
            confidence=0.9,
            status=Confidence.KNOWN,
        ),
    )
    _mint(
        workspace,
        db,
        scope,
        Insight(
            id="p2",
            statement="s",
            trigger="asserted declared uninvoked",
            cues=["c2"],
            confidence=0.9,
            status=Confidence.KNOWN,
        ),
    )
    idx = StructuralIndex(
        db,
        workspace,
        scope,
        embedder=emb,
        config=MemoryConfig.for_tier(tier),  # type: ignore[arg-type]
    )
    await idx.trigger_index()
    return idx, emb


async def test_personal_skips_rerank_when_margin_wide(workspace, db, scope) -> None:
    idx, _ = await _two_insight_index(workspace, db, scope, "personal")
    rr = RecordingReranker()
    # Only p1's cue lights -> a single candidate, so the margin is wide (no rival).
    await idx.match(Situation(text="x", summary="asserted unwired", cues=["c1"]), reranker=rr)
    assert rr.calls == 0  # personal + wide margin -> rerank skipped


async def test_enterprise_reranks_bounded_candidate_set(workspace, db, scope) -> None:
    idx, _ = await _two_insight_index(workspace, db, scope, "enterprise")
    rr = RecordingReranker()
    # Both cues light -> both promoted -> a two-candidate set to rerank.
    result = await idx.match(
        Situation(text="x", summary="asserted declared", cues=["c1", "c2"]), reranker=rr
    )
    assert rr.calls == 1  # enterprise -> always rerank the small set
    assert len(result.recalls) == 2
    assert rr.batch_sizes[0] == len(result.recalls)
    # The reranker verdict never leaks into agent-visible content.
    for recall in result.recalls:
        assert "rerank" not in recall.content.lower()
