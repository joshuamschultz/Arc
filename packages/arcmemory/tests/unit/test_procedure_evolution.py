"""Procedures EVOLVE — a re-extract merges into the card instead of truncating it.

The defect these tests pin: session one built an eight-step method, session two
mentioned two of those steps, and the card was rewritten to just those two — six
steps of accumulated knowledge destroyed silently. A procedure is the USER's way of
doing something (a mini-skill the agent edits across sessions), so the distiller is
handed the existing card and its answer is MERGED: mere omission never deletes a
step, only an explicit ``dropped_steps`` does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import ProcedureCandidate, ProcedureExtraction, extract_procedures
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.tools import build_memory_tools
from arcmemory.types import Event, Procedure, Scope

_CALLER = "did:arc:default:memory/deadbeef"

_SEO_STEPS = [
    "pull the seed keyword list from [[ahrefs]]",
    "score each keyword by volume / difficulty",
    "check the top 10 SERP results for intent",
    "log the winning intent in the research sheet",
    "draft the outline against the winning intent",
    "run the outline past [[acme]] for brand fit",
    "queue the brief for the writer",
    "recheck rankings 30 days after publish",
]


class _FakeProcedureDistiller:
    """Returns fixtured procedures and records the existing cards it was handed."""

    def __init__(self, extraction: ProcedureExtraction) -> None:
        self._extraction = extraction
        self.seen_existing: list[list[Procedure]] = []

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        self.seen_existing.append(existing)
        return self._extraction


def _events(scope: Scope) -> list[Event]:
    return [Event(event_id="e0", scope=scope.key, kind="user", text="did the seo research again")]


def _tool(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


# -- the store: merge is the single write funnel ---------------------------


def test_upsert_keeps_steps_the_distiller_did_not_mention(workspace: Path) -> None:
    store = ProceduralStore(workspace)
    store.upsert("seo-research", "SEO research", steps=_SEO_STEPS)

    store.upsert("seo-research", "SEO research", steps=[_SEO_STEPS[2], _SEO_STEPS[4]])

    loaded = store.read("seo-research")
    assert loaded is not None
    # Unmentioned steps survive IN THEIR ORIGINAL POSITION — the card stays runnable.
    assert loaded.steps == _SEO_STEPS
    assert loaded.use_count == 2


def test_upsert_removes_only_explicitly_dropped_steps(workspace: Path) -> None:
    store = ProceduralStore(workspace)
    store.upsert("seo-research", "SEO research", steps=_SEO_STEPS)

    store.upsert(
        "seo-research",
        "SEO research",
        steps=[_SEO_STEPS[0]],
        dropped=[_SEO_STEPS[6]],
    )

    loaded = store.read("seo-research")
    assert loaded is not None
    assert _SEO_STEPS[6] not in loaded.steps
    assert loaded.steps == [s for s in _SEO_STEPS if s != _SEO_STEPS[6]]


def test_upsert_honors_reordering_rewording_and_insertion(workspace: Path) -> None:
    store = ProceduralStore(workspace)
    store.upsert("brief", "Brief", steps=["a", "b", "c"])

    store.upsert("brief", "Brief", steps=["c", "b-plus", "a"], dropped=["b"])

    loaded = store.read("brief")
    assert loaded is not None
    assert loaded.steps == ["c", "b-plus", "a"]


def test_upsert_keeps_the_existing_trigger_when_the_incoming_one_is_blank(
    workspace: Path,
) -> None:
    store = ProceduralStore(workspace)
    store.upsert("seo-research", "SEO research", when_to_use="asked to research SEO", steps=["a"])

    store.upsert("seo-research", "", steps=["b"])

    loaded = store.read("seo-research")
    assert loaded is not None
    assert loaded.when_to_use == "asked to research SEO"
    assert loaded.title == "SEO research"


# -- the distiller seam: it must SEE the card it is merging into -----------


async def test_extract_procedures_hands_the_existing_card_to_the_distiller(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    store = ProceduralStore(workspace)
    store.upsert("seo-research", "SEO research", when_to_use="seo work", steps=_SEO_STEPS)
    distiller = _FakeProcedureDistiller(ProcedureExtraction())

    await extract_procedures(
        _events(scope),
        distiller=distiller,
        store=store,
        config=MemoryConfig(),
        graph=WeightedGraph(db),
        scope=scope,
    )

    handed = distiller.seen_existing[0]
    assert [p.slug for p in handed] == ["seo-research"]
    assert handed[0].steps == _SEO_STEPS  # title, trigger and CURRENT steps, not just the slug
    assert handed[0].when_to_use == "seo work"


async def test_eight_step_method_survives_a_two_step_session(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    """The headline regression: six steps must not vanish by mere omission."""
    store = ProceduralStore(workspace)
    store.upsert("seo-research", "SEO research", steps=_SEO_STEPS)
    distiller = _FakeProcedureDistiller(
        ProcedureExtraction(
            procedures=[
                ProcedureCandidate(
                    slug="seo-research",
                    title="SEO research",
                    steps=[_SEO_STEPS[1], _SEO_STEPS[5]],
                )
            ]
        )
    )

    await extract_procedures(
        _events(scope),
        distiller=distiller,
        store=store,
        config=MemoryConfig(),
        graph=WeightedGraph(db),
        scope=scope,
    )

    loaded = store.read("seo-research")
    assert loaded is not None
    assert loaded.steps == _SEO_STEPS
    assert len(loaded.steps) == 8


# -- the graph: a procedure is a reachable node ----------------------------


async def test_procedure_slug_becomes_a_reachable_graph_node(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    graph = WeightedGraph(db)
    store = ProceduralStore(workspace)
    distiller = _FakeProcedureDistiller(
        ProcedureExtraction(
            procedures=[
                ProcedureCandidate(
                    slug="seo-research",
                    title="SEO research",
                    when_to_use="asked to research SEO for [[acme]]",
                    steps=_SEO_STEPS,
                )
            ]
        )
    )

    await extract_procedures(
        _events(scope),
        distiller=distiller,
        store=store,
        config=MemoryConfig(),
        graph=graph,
        scope=scope,
    )

    neighbors = dict(graph.neighbors(scope.key, "seo-research"))
    assert "acme" in neighbors  # from when_to_use AND a step
    assert "ahrefs" in neighbors  # from a step
    # The procedure is reachable FROM the entity too (spreading activation is undirected).
    assert "seo-research" in dict(graph.neighbors(scope.key, "acme"))


# -- the agentic engine writes through the same funnel ---------------------


async def test_list_procedures_shows_the_agent_what_methods_exist(
    workspace: Path, db: MemoryDB
) -> None:
    tools = build_memory_tools(
        workspace=workspace, db=db, config=MemoryConfig(), caller_did=_CALLER
    )
    assert "no procedures" in await _tool(tools, "list_procedures").execute({})
    ProceduralStore(workspace).upsert(
        "seo-research", "SEO research", when_to_use="asked to research SEO", steps=_SEO_STEPS
    )

    listing = await _tool(tools, "list_procedures").execute({})

    assert "seo-research" in listing
    assert "SEO research" in listing
    assert "asked to research SEO" in listing  # the trigger, so the agent can match on it


async def test_read_procedure_shows_the_full_card_or_says_it_is_absent(
    workspace: Path, db: MemoryDB
) -> None:
    tools = build_memory_tools(
        workspace=workspace, db=db, config=MemoryConfig(), caller_did=_CALLER
    )
    assert "no such procedure" in await _tool(tools, "read_procedure").execute({"slug": "ghost"})
    ProceduralStore(workspace).upsert(
        "seo-research", "SEO research", when_to_use="asked to research SEO", steps=_SEO_STEPS
    )

    card = await _tool(tools, "read_procedure").execute({"slug": "seo-research"})

    assert "asked to research SEO" in card
    # Every step, numbered — the agent cannot reorder or reword what it cannot see.
    for i, step in enumerate(_SEO_STEPS, start=1):
        assert f"{i}. {step}" in card


async def test_agentic_engine_can_read_then_deliberately_reorder_and_reword(
    workspace: Path, db: MemoryDB
) -> None:
    """With sight of the card, drop/reword/reorder become the model's judgment, not luck."""
    tools = build_memory_tools(
        workspace=workspace, db=db, config=MemoryConfig(), caller_did=_CALLER
    )
    record = _tool(tools, "record_procedure")
    await record.execute(
        {"slug": "brief", "title": "Brief", "steps": ["draft outline", "review", "send"]}
    )

    card = await _tool(tools, "read_procedure").execute({"slug": "brief"})
    assert "2. review" in card
    # Reads the card, then rewrites it wholesale: reordered, one step reworded.
    await record.execute(
        {
            "slug": "brief",
            "title": "Brief",
            "steps": ["draft outline", "send", "review with [[acme]]"],
            "dropped_steps": ["review"],
        }
    )

    loaded = ProceduralStore(workspace).read("brief")
    assert loaded is not None
    assert loaded.steps == ["draft outline", "send", "review with [[acme]]"]


async def test_record_procedure_tool_merges_and_links(workspace: Path, db: MemoryDB) -> None:
    tools = build_memory_tools(
        workspace=workspace, db=db, config=MemoryConfig(), caller_did=_CALLER
    )
    tool = _tool(tools, "record_procedure")
    await tool.execute({"slug": "seo-research", "title": "SEO research", "steps": _SEO_STEPS})

    await tool.execute({"slug": "seo-research", "title": "SEO research", "steps": [_SEO_STEPS[3]]})

    loaded = ProceduralStore(workspace).read("seo-research")
    assert loaded is not None
    assert loaded.steps == _SEO_STEPS
    scope = Scope(agent_did=_CALLER)
    assert "ahrefs" in dict(WeightedGraph(db).neighbors(scope.key, "seo-research"))
