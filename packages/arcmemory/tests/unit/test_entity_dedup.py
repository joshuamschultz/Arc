"""Entity identity de-dup engine (arcmemory.entity_dedup) over real-world pairs.

Every pair here is a duplicate a live agent's Knowledge view showed side by side:
"Thesis 5" next to "Thesis 5: Multi-Layer Tuning", a business-idea next to the same
project, an insight next to a note about the same framework. The engine blocks on
normalized name keys and series numbers (no embedder needed), compares across
types, folds near-certain series pairs deterministically, and sends the rest to
the LLM confirmer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.entity_dedup import EntityDeduper, dedup_agent_memory, name_tokens
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Scope


class RecordingConfirmer:
    """Confirms every candidate group; records what it was asked."""

    def __init__(self) -> None:
        self.groups: list[list[str]] = []

    async def confirm_entity_merges(self, groups: list[Any]) -> list[list[str]]:
        asked = [[ref.slug for ref in group] for group in groups]
        self.groups += asked
        return asked

    async def find_contradictions(self, group: list[Any]) -> list[str]:
        return []


class RejectingConfirmer(RecordingConfirmer):
    async def confirm_entity_merges(self, groups: list[Any]) -> list[list[str]]:
        await super().confirm_entity_merges(groups)
        return []


class KeywordEmbedder:
    """Names sharing a keyword embed identically (cosine 1); others are orthogonal."""

    _KEYWORDS: ClassVar[list[str]] = ["thesis", "austin", "source"]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if kw in t.lower() else 0.0 for kw in self._KEYWORDS] + [0.01] for t in texts]


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, action: str, target: str, extra: dict[str, Any]) -> None:
        self.events.append((action, target, extra))

    def actions(self) -> list[str]:
        return [a for a, _, _ in self.events]


def _store(workspace: Path, db: MemoryDB, scope: Scope) -> tuple[SemanticStore, WeightedGraph]:
    graph = WeightedGraph(db)
    return SemanticStore(workspace, graph, scope=scope.key), graph


def _card(
    store: SemanticStore,
    slug: str,
    name: str,
    entity_type: str,
    *,
    facts: int = 1,
    classification: str = "unclassified",
    tags: list[str] | None = None,
) -> None:
    for i in range(facts):
        store.write_fact(
            slug,
            f"p{i}",
            f"{name} value {i}",
            name=name,
            entity_type=entity_type,
            classification=classification,
            tags=tags,
        )


def _deduper(
    store: SemanticStore,
    graph: WeightedGraph,
    scope: Scope,
    *,
    embedder: Any = None,
    confirmer: Any = None,
    tier: str = "personal",
    emit: Any = None,
) -> EntityDeduper:
    return EntityDeduper(
        store,
        graph,
        scope.key,
        config=MemoryConfig.for_tier(tier),  # type: ignore[arg-type]
        embedder=embedder,
        confirmer=confirmer,
        emit=emit,
    )


# -- name keys ------------------------------------------------------------------


def test_name_tokens_strip_punctuation_stopwords_and_plurals() -> None:
    assert name_tokens("Thesis 4: Execution Shifts to Agents / Plan→Execute→Validate") >= {
        "execution",
        "shift",
        "agent",
        "plan",
        "validate",
    }
    assert name_tokens("Data as your Moat") == {"data", "moat"}
    assert name_tokens("Self-Improving") == name_tokens("Self Improving")


# -- deterministic series merges ------------------------------------------------


async def test_thesis_5_bare_label_folds_into_the_named_card_without_an_llm(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing", tags=["thesis"])
    _card(
        store,
        "thesis-5-multi-layer-tuning",
        "Thesis 5: Multi-Layer Tuning (LoRA, GRPO, SFT…)",
        "thesis",
    )

    result = await _deduper(store, graph, scope).run(apply=True)

    assert len(result.merged) == 1
    [slug] = store.slugs()
    card = store.read(slug)
    assert card is not None
    assert card.name == "Thesis 5: Multi-Layer Tuning (LoRA, GRPO, SFT…)"  # richer name
    assert card.entity_type == "thesis"  # more specific type
    assert "Thesis 5" in card.aliases  # the old name still resolves
    assert "thesis" not in card.tags
    assert {f.predicate for f in card.facts} == {"p0"}


async def test_thesis_4_two_spellings_fold_deterministically(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(
        store,
        "thesis-4-execution-shifts",
        "Thesis 4: Execution Shifts to Agents / Plan→Execute→Validate",
        "thing",
        facts=2,
    )
    _card(
        store,
        "thesis-4-execution-shifts-to-agents-humans-plan-validate",
        "Thesis 4 Execution Shifts To Agents Humans Plan Validate",
        "unknown",
    )

    result = await _deduper(store, graph, scope).run(apply=True)

    assert result.merged == [
        ("thesis-4-execution-shifts-to-agents-humans-plan-validate", "thesis-4-execution-shifts")
    ]
    card = store.read("thesis-4-execution-shifts")
    assert card is not None
    assert card.entity_type == "thesis"  # the series label names the kind
    assert card.name == "Thesis 4: Execution Shifts to Agents / Plan→Execute→Validate"


async def test_different_thesis_numbers_are_never_candidates(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-4", "Thesis 4: Execution Shifts", "thesis")
    _card(store, "thesis-5", "Thesis 5: Execution Tuning", "thesis")
    confirmer = RecordingConfirmer()

    result = await _deduper(
        store, graph, scope, embedder=KeywordEmbedder(), confirmer=confirmer
    ).run(apply=True)

    assert result.merged == []
    assert confirmer.groups == []  # identical embeddings, but different series members


async def test_a_bare_label_with_two_different_members_is_left_to_the_llm(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing")
    _card(store, "thesis-5-alpha", "Thesis 5: Alpha Beta Gamma", "thesis")
    _card(store, "thesis-5-delta", "Thesis 5: Delta Epsilon Zeta", "thesis")

    plan = await _deduper(store, graph, scope).plan()

    assert plan.certain == []
    assert [sorted(c) for c in plan.ambiguous] == [
        ["thesis-5", "thesis-5-alpha", "thesis-5-delta"]
    ]


async def test_series_number_with_unrelated_words_needs_confirmation(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "hf-incident-thesis12", "HF Security Incident (Thesis 12 Evidence)", "thing")
    _card(store, "thesis-12", "Thesis 12: Self-Hosted Inference Necessity", "thesis")
    confirmer = RejectingConfirmer()

    result = await _deduper(store, graph, scope, confirmer=confirmer).run(apply=True)

    assert result.plan.certain == []
    assert [sorted(g) for g in confirmer.groups] == [["hf-incident-thesis12", "thesis-12"]]
    assert result.merged == []
    assert len(store.slugs()) == 2


# -- cross-type candidates confirmed by the LLM ---------------------------------


async def test_vertical_saas_business_idea_and_project_merge_keeping_project(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    _card(
        store,
        "vertical-agentic-saas",
        "Vertical Agentic SaaS/System of Record for Manufacturing & Supply Chain",
        "business-idea",
    )
    _card(
        store,
        "vertical-agentic-manufacturing-saas",
        "Vertical Agentic Manufacturing SaaS",
        "project",
        facts=2,
    )
    confirmer = RecordingConfirmer()

    result = await _deduper(store, graph, scope, confirmer=confirmer).run(apply=True)

    assert result.merged == [("vertical-agentic-saas", "vertical-agentic-manufacturing-saas")]
    card = store.read("vertical-agentic-manufacturing-saas")
    assert card is not None
    assert card.entity_type == "project"
    assert card.name == "Vertical Agentic SaaS/System of Record for Manufacturing & Supply Chain"
    assert "Vertical Agentic Manufacturing SaaS" in card.aliases


async def test_blomfield_insight_and_note_are_compared_across_types(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(
        store,
        "tom-blomfield-self-improving-company-framework",
        "Tom Blomfield Self-Improving Company Framework",
        "insight",
    )
    _card(
        store,
        "tom-blomfield-self-improving-company-loop",
        "Tom Blomfield Self Improving Company Loop",
        "note",
    )
    confirmer = RecordingConfirmer()

    result = await _deduper(store, graph, scope, confirmer=confirmer).run(apply=True)

    assert len(result.merged) == 1
    [slug] = store.slugs()
    card = store.read(slug)
    assert card is not None
    assert card.entity_type == "concept"


async def test_data_moat_trio_series_pair_is_certain_and_the_third_is_asked(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "ai-concept-1", "AI Concept 1: Data as your Moat", "project", facts=2)
    _card(store, "ai-concept-1-data-moat-stack", "Ai Concept 1 Data Moat Stack", "unknown")
    _card(store, "ai-concept-data-as-moat", "Data as your Moat", "concept")

    plan = await _deduper(store, graph, scope).plan()

    assert [(g.survivor, sorted(g.folded)) for g in plan.certain] == [
        ("ai-concept-1", ["ai-concept-1-data-moat-stack"])
    ]
    assert [sorted(c) for c in plan.ambiguous] == [["ai-concept-1", "ai-concept-data-as-moat"]]


async def test_the_same_name_cluster_takes_the_contradiction_path(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "pantex-writeup", "Pantex Writeup", "document")
    _card(store, "pantex-write-up", "Pantex Writeup", "document")
    confirmer = RejectingConfirmer()  # would decline the OPEN question

    result = await _deduper(store, graph, scope, confirmer=confirmer).run(apply=True)

    assert len(result.merged) == 1  # no contradiction -> folded
    assert confirmer.groups == []  # never asked the open question


# -- guards -----------------------------------------------------------------------


async def test_identity_kinds_never_merge_even_when_confirmed(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "austin-place", "Austin", "place")
    _card(store, "austin-person", "Austin", "person")
    confirmer = RecordingConfirmer()

    result = await _deduper(
        store, graph, scope, embedder=KeywordEmbedder(), confirmer=confirmer
    ).run(apply=True)

    assert result.merged == []
    assert confirmer.groups == []


async def test_system_cards_are_never_deduped(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    store.write_fact("source-aa", "kind", "jira", name="Source Aa", entity_type="source")
    store.write_fact("source-ab", "kind", "jira", name="Source Aa", entity_type="source")
    confirmer = RecordingConfirmer()

    result = await _deduper(
        store, graph, scope, embedder=KeywordEmbedder(), confirmer=confirmer
    ).run(apply=True)

    assert result.merged == []
    assert sorted(store.slugs()) == ["source-aa", "source-ab"]


async def test_never_merges_across_classification_levels(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-7", "Thesis 7", "thing", classification="cui")
    _card(store, "thesis-7-byoa", "Thesis 7: Bring Your Own Agent", "thesis")

    result = await _deduper(store, graph, scope, confirmer=RecordingConfirmer()).run(apply=True)

    assert result.merged == []
    assert sorted(store.slugs()) == ["thesis-7", "thesis-7-byoa"]
    assert result.plan.blocked == [["thesis-7", "thesis-7-byoa"]]


async def test_federal_fails_closed_on_an_unknown_classification_label(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-7", "Thesis 7", "thing", classification="stable")
    _card(store, "thesis-7-byoa", "Thesis 7: Bring Your Own Agent", "thesis")

    federal = await _deduper(store, graph, scope, tier="federal").run(apply=True)
    assert federal.merged == []

    personal = await _deduper(store, graph, scope, tier="personal").run(apply=True)
    assert len(personal.merged) == 1  # unknown label reads as unclassified off-federal


async def test_store_merge_primitive_refuses_a_cross_level_fold(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)
    _card(store, "a", "Alpha Card", "project", classification="secret")
    _card(store, "b", "Alpha Card", "project", classification="unclassified")

    assert store.merge_into("b", "a", strict=False) is False
    assert sorted(store.slugs()) == ["a", "b"]


# -- non-lossy, link-rewriting, audited, idempotent -----------------------------


async def test_merge_rewrites_inbound_links_in_other_cards(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing")
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    store.write_fact(
        "ai-theses", "includes", "see [[thesis-5]]", name="AI Theses", entity_type="project"
    )

    await _deduper(store, graph, scope).run(apply=True)

    hub = store.read("ai-theses")
    assert hub is not None
    assert "[[thesis-5-tuning]]" in hub.links_to
    assert "[[thesis-5]]" not in hub.links_to
    assert hub.facts[0].value == "see [[thesis-5-tuning]]"
    assert "thesis-5-tuning" in {n for n, _ in graph.neighbors(scope.key, "ai-theses")}


async def test_merge_unions_facts_tags_and_keeps_aliases_resolvable(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-9", "Thesis 9", "thing", tags=["thesis", "productivity"])
    store.write_fact("thesis-9", "extra", "only-on-the-bare-card", name="Thesis 9")
    _card(
        store, "thesis-9-multiplier", "Thesis 9: AI as Productivity Multiplier", "thesis", facts=3
    )

    await _deduper(store, graph, scope).run(apply=True)

    card = store.read("thesis-9-multiplier")
    assert card is not None
    assert {"p0", "p1", "p2", "extra"} <= {f.predicate for f in card.facts}
    assert card.tags == ["productivity"]
    assert store.resolve("thesis-9") == "thesis-9-multiplier"


async def test_each_fold_writes_an_audited_merge_record(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing")
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    recorder = Recorder()

    await _deduper(store, graph, scope, emit=recorder).run(apply=True)

    merged = [extra for action, _, extra in recorder.events if action == "memory.entity_merged"]
    assert merged == [
        {
            "survivor": "thesis-5-tuning",
            "folded": "thesis-5",
            "basis": "series",
            "entity_type": "thesis",
            "folded_name": "Thesis 5",
        }
    ]
    assert "memory.dedup_pass" in recorder.actions()
    log = workspace / "memory" / "merge-log.jsonl"
    [line] = log.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["survivor"] == "thesis-5-tuning"
    assert record["folded"] == "thesis-5"
    assert record["folded_name"] == "Thesis 5"


async def test_a_second_pass_is_a_no_op(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing")
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    _card(store, "vertical-saas", "Vertical Agentic SaaS for Manufacturing", "business-idea")
    _card(store, "vertical-mfg-saas", "Vertical Agentic Manufacturing SaaS", "project")
    deduper = _deduper(store, graph, scope, confirmer=RecordingConfirmer())

    first = await deduper.run(apply=True)
    files = {
        p.name: p.read_text(encoding="utf-8")
        for p in (workspace / "memory" / "entities").glob("*.md")
    }
    second = await deduper.run(apply=True)

    assert len(first.merged) == 2
    assert second.merged == []
    assert second.plan.certain == [] and second.plan.ambiguous == []
    after = {
        p.name: p.read_text(encoding="utf-8")
        for p in (workspace / "memory" / "entities").glob("*.md")
    }
    assert after == files


async def test_dry_run_plans_but_writes_nothing(workspace, db, scope) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing")
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    confirmer = RecordingConfirmer()

    result = await _deduper(store, graph, scope, confirmer=confirmer).run(apply=False)

    assert result.merged == []
    assert len(result.plan.certain) == 1
    assert sorted(store.slugs()) == ["thesis-5", "thesis-5-tuning"]
    assert confirmer.groups == []  # a dry run spends no LLM call
    assert not (workspace / "memory" / "merge-log.jsonl").exists()


async def test_no_embedder_still_runs_the_deterministic_channel_loudly(
    workspace, db, scope
) -> None:
    store, graph = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing")
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    recorder = Recorder()

    result = await _deduper(store, graph, scope, emit=recorder).run(apply=True)

    assert len(result.merged) == 1
    skipped = [e for a, _, e in recorder.events if a == "memory.dedup_skipped"]
    assert {"reason": "no-embedder"} in skipped


# -- the operator entry point (arc memory dedup --agent) -----------------------


async def test_agent_dedup_dry_run_plans_kinds_and_merges_without_writing(
    workspace, db, scope
) -> None:
    store, _ = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing", facts=1)
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    before = sorted(p.read_text() for p in (workspace / "memory" / "entities").glob("*.md"))

    report = await dedup_agent_memory(workspace, scope.agent_did, apply=False)

    assert len(report.result.plan.certain) == 1
    assert report.result.merged == []
    after = sorted(p.read_text() for p in (workspace / "memory" / "entities").glob("*.md"))
    assert after == before


async def test_agent_dedup_apply_merges_and_audits(workspace, db, scope) -> None:
    store, _ = _store(workspace, db, scope)
    _card(store, "thesis-5", "Thesis 5", "thing", facts=1)
    _card(store, "thesis-5-tuning", "Thesis 5: Multi-Layer Tuning", "thesis", facts=2)
    sink = _Sink()

    report = await dedup_agent_memory(workspace, scope.agent_did, apply=True, audit_sink=sink)

    assert report.result.merged == [("thesis-5", "thesis-5-tuning")]
    assert store.slugs() == ["thesis-5-tuning"]
    actions = [e.action for e in sink.events]
    assert "memory.entity_merged" in actions and "memory.dedup_pass" in actions
    assert all(e.actor_did == scope.agent_did for e in sink.events)


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)
