"""T-052/053/054 — consolidation ("sleep"): orchestration, audit chain, cue merge."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from arctrust.audit import WormSink
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    DaySummaryDraft,
    FactCandidate,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    ProcedureCandidate,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Scope

_NOW = datetime(2026, 7, 7, tzinfo=UTC)
_30_DAYS_AGO = "2026-06-07T00:00:00+00:00"


class FakeDistiller:
    """Injected structured-completion stub returning fixtured facts + insights."""

    def __init__(self, extraction: FactExtraction, mint: InsightMint) -> None:
        self._extraction = extraction
        self._mint = mint

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return self._extraction

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return self._mint

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction(
            procedures=[
                ProcedureCandidate(
                    slug="check-gauge", title="Check the gauge", steps=["open valve", "read gauge"]
                )
            ]
        )

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft(timeline=["09:00 the day happened"], people=["Alice"])

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        return None


class RaisingDistiller:
    """Succeeds at facts, then crashes minting — simulates a mid-run failure."""

    def __init__(self, extraction: FactExtraction) -> None:
        self._extraction = extraction

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return self._extraction

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        raise RuntimeError("boom mid-consolidation")

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction(
            procedures=[
                ProcedureCandidate(
                    slug="check-gauge", title="Check the gauge", steps=["open valve", "read gauge"]
                )
            ]
        )

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()


def _seed_day(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    """A fixture day: a prior fact to contradict, an action loop, decay-able edges."""
    semantic = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    semantic.write_fact("alice", "role", "engineer", confidence=0.6)  # to be contradicted

    episodic = EpisodicStore(db, workspace)
    events = [
        # Conversation turns the distiller consolidates.
        Event(
            event_id="a0",
            scope=scope.key,
            kind="respond",
            text="open valve",
            ts="2026-07-07T00:00:00+00:00",
        ),
        Event(
            event_id="a1",
            scope=scope.key,
            kind="respond",
            text="check gauge",
            ts="2026-07-07T00:00:01+00:00",
        ),
        Event(
            event_id="b0",
            scope=scope.key,
            kind="respond",
            text="boundary",
            ts="2026-07-07T00:00:02+00:00",
        ),
        Event(
            event_id="a2",
            scope=scope.key,
            kind="respond",
            text="open valve",
            ts="2026-07-07T00:00:03+00:00",
        ),
        Event(
            event_id="a3",
            scope=scope.key,
            kind="respond",
            text="check gauge",
            ts="2026-07-07T00:00:04+00:00",
        ),
    ]
    for ev in events:
        episodic.append(ev)

    graph = WeightedGraph(db)
    # A neutral edge last hit 30 days ago -> should decay below the floor.
    graph.hebbian_bump(scope.key, "stale-x", "stale-y", ts=_30_DAYS_AGO)
    # A salient edge, same age -> salience slows decay, it survives.
    graph.hebbian_bump(scope.key, "vital-x", "vital-y", salience=1.0, ts=_30_DAYS_AGO)


def _distiller() -> FakeDistiller:
    return FakeDistiller(
        FactExtraction(facts=[FactCandidate(slug="alice", predicate="role", value="manager")]),
        InsightMint(
            insights=[
                InsightCandidate(
                    id="loop-insight",
                    statement="valve-then-gauge is a recurring check",
                    trigger="a resource is engaged then its state is verified",
                    cues=["engage-then-verify"],
                    instances=["a0", "a1"],
                )
            ]
        ),
    )


def _consolidator(workspace, db, scope, distiller, *, sink=None) -> Consolidator:
    return Consolidator(
        db, workspace, scope, distiller=distiller, config=MemoryConfig(), audit_sink=sink
    )


# -- T-052: end-to-end over a fixture day -----------------------------------


async def test_consolidation_end_to_end(workspace, db, scope) -> None:
    _seed_day(workspace, db, scope)
    result = await _consolidator(workspace, db, scope, _distiller()).run(now=_NOW)

    # Fact updated with a `was:` trail (additive, not overwrite).
    fact = next(
        f
        for f in SemanticStore(workspace, WeightedGraph(db), scope=scope.key).read("alice").facts
        if f.predicate == "role"
    )
    assert fact.value == "manager" and fact.was_value == "engineer"
    assert result.facts_updated == 1

    # Insight minted (guessed).
    insight = InsightStore(workspace).read("loop-insight")
    assert insight is not None and insight.status.value == "guessed"
    assert result.insights_minted == 1

    # Procedure promoted (valve->gauge seen twice).
    assert result.procedures_promoted == 1

    # Stale edge decayed out; salient edge kept.
    graph = WeightedGraph(db)
    assert graph.weight(scope.key, "stale-x", "stale-y") == 0.0
    assert graph.weight(scope.key, "vital-x", "vital-y") > 0.0
    assert result.edges_decayed >= 1


async def test_crash_mid_write_leaves_consistent_manifest(workspace, db, scope) -> None:
    _seed_day(workspace, db, scope)
    distiller = RaisingDistiller(
        FactExtraction(facts=[FactCandidate(slug="alice", predicate="role", value="manager")])
    )
    consolidator = _consolidator(workspace, db, scope, distiller)

    with pytest.raises(RuntimeError, match="boom"):
        await consolidator.run(now=_NOW)

    # The crash marker survives and is internally consistent (valid JSON, in-progress).
    manifest_path = workspace / "memory" / ".consolidate-manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "in_progress"

    # Files written before the crash are atomically valid (the fact landed).
    fact = next(
        f
        for f in SemanticStore(workspace, WeightedGraph(db), scope=scope.key).read("alice").facts
        if f.predicate == "role"
    )
    assert fact.value == "manager"

    # A fresh consolidator sees the pending manifest and recovers deterministically.
    recovered = _consolidator(workspace, db, scope, _distiller())
    assert recovered.pending_recovery
    await recovered.recover()
    assert not manifest_path.exists()


# -- cadence: consolidation runs on an interval, not every turn -------------


async def test_run_stamps_last_run_and_gates_by_interval(workspace, db, scope) -> None:
    _seed_day(workspace, db, scope)
    consolidator = _consolidator(workspace, db, scope, _distiller())

    # Never consolidated -> due immediately.
    assert consolidator.due(now=_NOW, interval_minutes=60)
    await consolidator.run(now=_NOW)

    # Just ran -> not due again until the full interval has elapsed.
    assert not consolidator.due(now=_NOW + timedelta(minutes=59), interval_minutes=60)
    assert consolidator.due(now=_NOW + timedelta(minutes=60), interval_minutes=60)


async def test_last_run_stamp_survives_a_fresh_consolidator(workspace, db, scope) -> None:
    """The cadence gate persists to disk, so an agent restart still respects it."""
    _seed_day(workspace, db, scope)
    await _consolidator(workspace, db, scope, _distiller()).run(now=_NOW)

    fresh = _consolidator(workspace, db, scope, _distiller())  # simulates a restart
    assert not fresh.due(now=_NOW + timedelta(minutes=1), interval_minutes=60)


# -- #23: input curation keeps tool plumbing out of distillation ------------


class _RecordingDistiller:
    """Records the event ids handed to each of the four distiller entry points."""

    def __init__(self) -> None:
        self.seen: dict[str, list[str]] = {
            "facts": [],
            "insights": [],
            "procedures": [],
            "day": [],
        }
        self.calls = 0

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        self.calls += 1
        self.seen["facts"] += [e.event_id for e in events]
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        self.calls += 1
        self.seen["insights"] += [e.event_id for e in events]
        return InsightMint()

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        self.calls += 1
        self.seen["procedures"] += [e.event_id for e in events]
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        self.calls += 1
        self.seen["day"] += [e.event_id for e in events]
        return DaySummaryDraft()


async def test_curation_keeps_tool_plumbing_out_of_every_distiller_input(workspace, db, scope) -> None:
    episodic = EpisodicStore(db, workspace)
    episodic.append(
        Event(
            event_id="plumb",
            scope=scope.key,
            kind="tool",  # mechanical plumbing, no entity tag
            text="tool call args echo",
            ts="2026-07-07T00:00:00+00:00",
        )
    )
    episodic.append(
        Event(
            event_id="said",
            scope=scope.key,
            kind="respond",  # what was actually observed
            text="alice shipped payments",
            ts="2026-07-07T00:00:01+00:00",
        )
    )
    distiller = _RecordingDistiller()

    await Consolidator(db, workspace, scope, distiller=distiller, config=MemoryConfig()).run(now=_NOW)

    for channel, ids in distiller.seen.items():
        assert "plumb" not in ids, f"tool plumbing leaked into {channel}"
        assert "said" in ids, f"real content missing from {channel}"
    # Curation is pure — it adds no LLM call (one per distiller entry point).
    assert distiller.calls == 4


# -- T-053: every mutation audited; the chain verifies ----------------------


async def test_every_mutation_is_audited_and_chain_verifies(
    workspace, db, scope, tmp_path
) -> None:
    _seed_day(workspace, db, scope)
    sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(generate_keypair().private_key))

    await _consolidator(workspace, db, scope, _distiller(), sink=sink).run(now=_NOW)

    actions = {
        json.loads(line)["event"]["action"]
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    }
    assert "memory.fact_updated" in actions
    assert "memory.insight_minted" in actions
    assert "memory.procedure_extracted" in actions
    assert "memory.edges_decayed" in actions
    assert "memory.file_rewritten" in actions
    assert sink.verify_chain()  # tamper-evident chain intact


# -- T-054: cue de-dup / merge bounds vocabulary drift ----------------------


class ClusterEmbedder:
    """Embeds cues so near-duplicate phrasings land on the same concept vector."""

    _CLUSTERS: ClassVar[dict[str, int]] = {"producer": 0, "wired": 1, "verify": 2}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0, 0.0, 0.0]
            for word, dim in self._CLUSTERS.items():
                if word in text.lower():
                    vec[dim] = 1.0
            out.append(vec)
        return out


async def test_cue_merge_repoints_instance_links(workspace, db, scope) -> None:
    store = InsightStore(workspace)
    graph = WeightedGraph(db)
    # Two insights whose cues are near-duplicate phrasings of the same concept.
    for iid, cue in [("i-a", "predicate-without-producer"), ("i-b", "predicate-lacks-producer")]:
        from arcmemory.types import Insight

        store.write(Insight(id=iid, statement="s", trigger="t", cues=[cue], instances=[iid]))
        graph.link(scope.key, iid, cue, kind="cue")

    consolidator = Consolidator(
        db,
        workspace,
        scope,
        distiller=_distiller(),
        config=MemoryConfig(),
        embedder=ClusterEmbedder(),
    )
    merges = await consolidator.merge_cues()

    assert merges, "expected a near-duplicate cue merge"
    # Both insights now reference the single canonical cue.
    cues_a = set(store.read("i-a").cues)
    cues_b = set(store.read("i-b").cues)
    canonical = cues_a & cues_b
    assert canonical, "both insights should share the merged canonical cue"
    # The merged cue's instance links (insight->cue edges) repointed onto canonical.
    canonical_cue = next(iter(canonical))
    linkers = {node for node, _ in graph.neighbors(scope.key, canonical_cue)}
    assert {"i-a", "i-b"} <= linkers


# -- entity de-dup: candidate generation -> LLM confirmation -> merge --------


class SubstringEmbedder:
    """One-hot NAME embedder: cards sharing a keyword land on the SAME vector.

    A keyword hit -> a 1 in that dim; two names sharing a keyword score cosine 1.0
    (>= the candidate threshold, so they cluster), while names on different keywords
    are orthogonal (cosine 0, never clustered). Deterministic + network-free.
    """

    _KEYWORDS: ClassVar[list[str]] = ["austin", "berlin", "josh", "acme"]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if kw in t.lower() else 0.0 for kw in self._KEYWORDS] for t in texts]


class RecordingConfirmer:
    """Records the candidate groups it is asked to confirm; confirms every one."""

    def __init__(self) -> None:
        self.groups: list[list[str]] = []

    async def confirm_entity_merges(self, groups: list) -> list[list[str]]:
        self.groups = [[ref.slug for ref in group] for group in groups]
        return [list(slugs) for slugs in self.groups]


class SubsetConfirmer:
    """Confirms ONLY a fixed subset of slugs as the same entity (the rest stay apart)."""

    def __init__(self, subset: set[str]) -> None:
        self._subset = subset

    async def confirm_entity_merges(self, groups: list) -> list[list[str]]:
        confirmed: list[list[str]] = []
        for group in groups:
            picked = [ref.slug for ref in group if ref.slug in self._subset]
            if len(picked) >= 2:
                confirmed.append(picked)
        return confirmed


class RejectingConfirmer:
    """Judges every candidate cluster NOT the same entity (records what it saw)."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def confirm_entity_merges(self, groups: list) -> list[list[str]]:
        self.seen = [[ref.slug for ref in group] for group in groups]
        return []


class RecordingSink:
    """Captures every emitted ``AuditEvent`` (the AuditSink write-seam)."""

    def __init__(self) -> None:
        self.events: list = []

    def write(self, event: object) -> None:
        self.events.append(event)

    def _reasons(self) -> list[str]:
        return [
            e.extra.get("reason")
            for e in self.events
            if getattr(e, "action", None) == "memory.dedup_skipped"
        ]


def _place(store: SemanticStore, slug: str, name: str, pred: str, value: str) -> None:
    store.write_fact(slug, pred, value, name=name, entity_type="place")


async def test_candidate_clustering_groups_similar_not_distinct(workspace, db, scope) -> None:
    """Similar same-type names cluster into ONE candidate group; a distinct card does not."""
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    _place(store, "austin-texas", "Austin, Texas", "state", "TX")
    _place(store, "austin-tx", "Austin, TX", "population", "1M")
    _place(store, "austin-metro", "Austin metro", "note", "hub")
    _place(store, "berlin", "Berlin", "country", "DE")  # distinct -> no neighbor -> no LLM

    confirmer = RecordingConfirmer()
    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=SubstringEmbedder(), confirmer=confirmer,
    )
    await consolidator.merge_entities()

    assert len(confirmer.groups) == 1
    assert set(confirmer.groups[0]) == {"austin-texas", "austin-tx", "austin-metro"}
    assert "berlin" not in confirmer.groups[0]  # no similar neighbor -> never sent to the LLM


async def test_only_llm_confirmed_subset_merges(workspace, db, scope) -> None:
    """A 3-card candidate cluster where the LLM confirms only 2 -> only those 2 fold."""
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    _place(store, "austin-texas", "Austin, Texas", "state", "TX")
    _place(store, "austin-tx", "Austin, TX", "population", "1M")
    _place(store, "austin-metro", "Austin metro", "note", "hub")

    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=SubstringEmbedder(),
        confirmer=SubsetConfirmer({"austin-texas", "austin-tx"}),
    )
    merges = await consolidator.merge_entities()

    assert merges == [("austin-tx", "austin-texas")]  # richest survivor keeps the card
    slugs = set(store.slugs())
    assert "austin-tx" not in slugs  # folded
    assert {"austin-texas", "austin-metro"} <= slugs  # survivor + unconfirmed both remain


async def test_confirmer_keeps_similar_but_different_people(workspace, db, scope) -> None:
    """Josh Schultz vs Joshua Shubbie: a candidate pair the LLM rejects -> NO merge."""
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("josh-schultz", "role", "founder", name="Josh Schultz", entity_type="person")
    store.write_fact("joshua-shubbie", "role", "artist", name="Joshua Shubbie",
                     entity_type="person")

    confirmer = RejectingConfirmer()
    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=SubstringEmbedder(), confirmer=confirmer,
    )
    merges = await consolidator.merge_entities()

    assert set(confirmer.seen[0]) == {"josh-schultz", "joshua-shubbie"}  # they WERE candidates
    assert merges == []  # ...but the LLM kept them apart
    assert set(store.slugs()) == {"josh-schultz", "joshua-shubbie"}


async def test_candidate_clusters_never_cross_type(workspace, db, scope) -> None:
    """Same name, different type never even becomes a candidate (place is not a person)."""
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("austin-place", "state", "TX", name="Austin", entity_type="place")
    store.write_fact("austin-person", "role", "eng", name="Austin", entity_type="person")

    confirmer = RecordingConfirmer()
    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=SubstringEmbedder(), confirmer=confirmer,
    )
    assert await consolidator.merge_entities() == []
    assert confirmer.groups == []  # cross-type pairs never reach the LLM
    assert set(store.slugs()) == {"austin-place", "austin-person"}


async def test_no_embedder_emits_loud_dedup_skipped(workspace, db, scope) -> None:
    """No embedder + real cards to dedup -> LOUD audit (never a silent [])."""
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    _place(store, "austin-texas", "Austin, Texas", "state", "TX")
    _place(store, "austin-tx", "Austin, TX", "population", "1M")

    sink = RecordingSink()
    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=None, confirmer=RecordingConfirmer(), audit_sink=sink,
    )
    assert await consolidator.merge_entities() == []
    assert "no-embedder" in sink._reasons()
    assert set(store.slugs()) == {"austin-texas", "austin-tx"}  # nothing merged


async def test_no_confirmer_skips_and_logs(workspace, db, scope, caplog) -> None:
    """Candidates found but no confirmer wired -> no merge, LOUD warn + audit."""
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    _place(store, "austin-texas", "Austin, Texas", "state", "TX")
    _place(store, "austin-tx", "Austin, TX", "population", "1M")

    sink = RecordingSink()
    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=SubstringEmbedder(), confirmer=None, audit_sink=sink,
    )
    with caplog.at_level(logging.WARNING):
        assert await consolidator.merge_entities() == []

    assert "no-confirmer" in sink._reasons()
    assert any("no-confirmer" in r.getMessage() for r in caplog.records)
    assert set(store.slugs()) == {"austin-texas", "austin-tx"}  # unconfirmed -> unmerged


async def test_confirmed_merge_is_non_lossy(workspace, db, scope) -> None:
    """A confirmed fold unions facts + records the loser as an alias + repoints edges."""
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=scope.key)
    _place(store, "austin-texas", "Austin, Texas", "state", "TX")
    _place(store, "austin-tx", "Austin, TX", "population", "1M")
    graph.link(scope.key, "austin-tx", "acme", kind="link")  # edge must follow the survivor

    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=SubstringEmbedder(), confirmer=RecordingConfirmer(),
    )
    merges = await consolidator.merge_entities()

    assert merges == [("austin-tx", "austin-texas")]
    survivor = store.read("austin-texas")
    assert {f.predicate for f in survivor.facts} == {"state", "population"}  # facts unioned
    assert "Austin, TX" in survivor.aliases or "austin-tx" in survivor.aliases  # alias trail
    assert "austin-tx" not in store.slugs()
    assert "austin-texas" in {n for n, _ in graph.neighbors(scope.key, "acme")}  # edge repointed
