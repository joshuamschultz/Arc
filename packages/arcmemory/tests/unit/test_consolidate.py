"""T-052/053/054 — consolidation ("sleep"): orchestration, audit chain, cue merge."""

from __future__ import annotations

import json
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
        return ProcedureExtraction()

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
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()


def _seed_day(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    """A fixture day: a prior fact to contradict, an action loop, decay-able edges."""
    semantic = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    semantic.write_fact("alice", "role", "engineer", confidence=0.6)  # to be contradicted

    episodic = EpisodicStore(db, workspace)
    events = [
        # A repeated action-sequence -> promotable procedure.
        Event(
            event_id="a0",
            scope=scope.key,
            kind="action",
            text="open valve",
            ts="2026-07-07T00:00:00+00:00",
        ),
        Event(
            event_id="a1",
            scope=scope.key,
            kind="action",
            text="check gauge",
            ts="2026-07-07T00:00:01+00:00",
        ),
        Event(
            event_id="b0",
            scope=scope.key,
            kind="obs",
            text="boundary",
            ts="2026-07-07T00:00:02+00:00",
        ),
        Event(
            event_id="a2",
            scope=scope.key,
            kind="action",
            text="open valve",
            ts="2026-07-07T00:00:03+00:00",
        ),
        Event(
            event_id="a3",
            scope=scope.key,
            kind="action",
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
            kind="obs",  # what was actually observed
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
    assert "memory.procedure_promoted" in actions
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


# -- entity de-dup / merge folds identity drift into one card ---------------


class EntityNameEmbedder:
    """Embeds entity NAMES so near-duplicate phrasings land on one vector."""

    _CLUSTERS: ClassVar[dict[str, int]] = {"austin": 0, "acme": 1, "berlin": 2}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0, 0.0, 0.0]
            for word, dim in self._CLUSTERS.items():
                if word in text.lower():
                    vec[dim] = 1.0
            out.append(vec)
        return out


async def test_merge_entities_folds_same_type_duplicates(workspace, db, scope) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=scope.key)
    # Two same-type cards for one place (drifted names) + one distinct place.
    store.write_fact("austin-texas", "state", "TX", confidence=0.9, name="Austin, Texas",
                     entity_type="place")
    store.write_fact("austin-tx", "population", "1M", confidence=0.8, name="Austin, TX",
                     entity_type="place")
    store.write_fact("berlin", "country", "DE", confidence=0.9, name="Berlin",
                     entity_type="place")
    graph.link(scope.key, "austin-tx", "acme", kind="link")  # edge must follow the merge

    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=EntityNameEmbedder(),
    )
    merges = await consolidator.merge_entities()

    assert merges == [("austin-tx", "austin-texas")]  # richer card (2 preds via fold) survives
    slugs = store.slugs()
    assert "austin-tx" not in slugs and "austin-texas" in slugs and "berlin" in slugs
    survivor = store.read("austin-texas")
    assert {f.predicate for f in survivor.facts} == {"state", "population"}  # facts folded
    # The duplicate's graph edge repointed onto the survivor.
    assert "austin-texas" in {n for n, _ in graph.neighbors(scope.key, "acme")}


async def test_merge_entities_never_crosses_type(workspace, db, scope) -> None:
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    # Same name, different type — a place must never fold into a person.
    store.write_fact("austin-place", "state", "TX", name="Austin", entity_type="place")
    store.write_fact("austin-person", "role", "eng", name="Austin", entity_type="person")

    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(),
        embedder=EntityNameEmbedder(),
    )
    assert await consolidator.merge_entities() == []
    assert set(store.slugs()) == {"austin-place", "austin-person"}


async def test_merge_entities_noop_without_embedder(workspace, db, scope) -> None:
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("austin-texas", "state", "TX", name="Austin, Texas", entity_type="place")
    store.write_fact("austin-tx", "population", "1M", name="Austin, TX", entity_type="place")

    consolidator = Consolidator(
        db, workspace, scope, distiller=_distiller(), config=MemoryConfig(), embedder=None
    )
    assert await consolidator.merge_entities() == []  # degrades cleanly
    assert set(store.slugs()) == {"austin-texas", "austin-tx"}
