"""Agentic consolidation engine: default engine writes; breach/timeout -> pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctrust.identity import AgentIdentity

from arcmemory.agent_consolidate import run_agentic_consolidation
from arcmemory.config import MemoryConfig
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import FactCandidate, FactExtraction, InsightMint
from arcmemory.index.graph import WeightedGraph
from arcmemory.react_adapter import ReactOutcome
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tools import MemoryTool
from arcmemory.types import Event, Scope
from tests.unit.test_consolidate import FakeDistiller

_NOW = datetime(2026, 7, 7, tzinfo=UTC)


def _seed_episode(db: MemoryDB, workspace: Path, scope: Scope) -> None:
    EpisodicStore(db, workspace).append(
        Event(
            event_id="e0",
            scope=scope.key,
            kind="respond",
            text="Brad Baker is the CTO of Acme.",
            ts="2026-07-07T09:00:00+00:00",
        )
    )


def _tool(tools: list[MemoryTool], name: str) -> MemoryTool:
    return next(t for t in tools if t.name == name)


async def test_agentic_engine_invokes_tools_and_writes_cards(
    workspace: Path, db: MemoryDB
) -> None:
    """A fake ReAct loop drives the memory tools; the card lands on disk."""

    async def fake_loop(*, tools: list[MemoryTool], **_kw: Any) -> ReactOutcome:
        await _tool(tools, "write_fact").execute(
            {"slug": "brad-baker", "predicate": "role", "value": "cto"}
        )
        return ReactOutcome(content="done", degraded=False, turns=1, tool_calls_made=1)

    identity = AgentIdentity.generate(org="default", agent_type="memory")
    scope = Scope(agent_did=identity.did)
    _seed_episode(db, workspace, scope)
    consolidator = Consolidator(
        db,
        workspace,
        scope,
        distiller=FakeDistiller(FactExtraction(), InsightMint()),
        config=MemoryConfig(),  # engine defaults to "agentic"
        model=object(),  # a model is present -> agentic path taken
        identity=identity,
        react_loop=fake_loop,
    )
    result = await consolidator.run(now=_NOW)
    entity = SemanticStore(workspace, WeightedGraph(db), scope=scope.key).read("brad-baker")
    assert entity is not None and any(f.predicate == "role" for f in entity.facts)
    assert result.window_events == 1


async def test_breach_falls_back_to_pipeline_no_data_loss(
    workspace: Path, db: MemoryDB
) -> None:
    """A breaching loop returns degraded; the pipeline distiller finishes the window."""

    async def breaching_loop(**_kw: Any) -> ReactOutcome:
        return ReactOutcome(degraded=True, reason="max_turns")

    scope = Scope(agent_did="did:arc:default:memory/abc")
    _seed_episode(db, workspace, scope)
    distiller = FakeDistiller(
        FactExtraction(
            facts=[FactCandidate(slug="brad-baker", predicate="role", value="cto", name="Brad Baker")]
        ),
        InsightMint(),
    )
    consolidator = Consolidator(
        db,
        workspace,
        scope,
        distiller=distiller,
        config=MemoryConfig(),
        model=object(),
        react_loop=breaching_loop,
    )
    result = await consolidator.run(now=_NOW)
    # The window still consolidated via the pipeline fallback.
    entity = SemanticStore(workspace, WeightedGraph(db), scope=scope.key).read("brad-baker")
    assert entity is not None and entity.facts
    assert result.facts_updated == 1


async def test_no_model_uses_pipeline(workspace: Path, db: MemoryDB) -> None:
    """With no model wired, the agentic engine is skipped for the pipeline distiller."""

    async def never(**_kw: Any) -> ReactOutcome:  # must not be called
        raise AssertionError("react loop must not run without a model")

    scope = Scope(agent_did="did:arc:default:memory/xyz")
    _seed_episode(db, workspace, scope)
    distiller = FakeDistiller(
        FactExtraction(
            facts=[FactCandidate(slug="brad-baker", predicate="role", value="cto")]
        ),
        InsightMint(),
    )
    consolidator = Consolidator(
        db, workspace, scope, distiller=distiller, config=MemoryConfig(), react_loop=never
    )
    result = await consolidator.run(now=_NOW)
    assert result.facts_updated == 1


async def test_arcrun_absent_falls_back_to_pipeline(
    workspace: Path, db: MemoryDB, monkeypatch: Any
) -> None:
    """The REAL adapter with arcrun 'uninstalled' degrades -> pipeline finishes the window."""
    from arcmemory import react_adapter

    monkeypatch.setattr(react_adapter, "_ARCRUN_AVAILABLE", False)
    scope = Scope(agent_did="did:arc:default:memory/noarcrun")
    _seed_episode(db, workspace, scope)
    distiller = FakeDistiller(
        FactExtraction(
            facts=[FactCandidate(slug="brad-baker", predicate="role", value="cto")]
        ),
        InsightMint(),
    )
    # No react_loop override -> the default run_react_loop adapter is used.
    consolidator = Consolidator(
        db, workspace, scope, distiller=distiller, config=MemoryConfig(), model=object()
    )
    result = await consolidator.run(now=_NOW)
    entity = SemanticStore(workspace, WeightedGraph(db), scope=scope.key).read("brad-baker")
    assert entity is not None and entity.facts  # pipeline ran despite arcrun absent
    assert result.facts_updated == 1


async def test_engine_empty_episodes_is_clean() -> None:
    result = await run_agentic_consolidation(
        episodes=[], model=object(), tools=[], config=MemoryConfig(), actor_did="did:arc:x"
    )
    assert result.degraded is False
